"""The chat turn's permission callback, end to end through the real handler.

``_stream_to_websocket_impl`` builds one ``can_use_tool`` closure per pooled
client. Every tool call the model makes outside an auto-allowing mode reaches
it; what it returns is what the runtime executes (only a class named
``PermissionResultAllow`` runs the tool — see ``HarnessAdapter.
_bridge_permission``). These tests capture the closure a real turn wires and
drive it the way the adapter does, answering prompts through the real
``permission_handler`` against the SQLite test DB.

The callback is captured by giving the turn a working directory that does not
exist: the engine refuses to spawn at that check, after the closure and its
refs are built, so the refs still hold this turn's broadcast function.
"""

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.core.connectors.manager  # noqa: F401 — see _next_prompt
import app.core.query_engine as qe
from app.core.permission_handler import _DeferSentinel, permission_handler
from app.core.provider.events import AssistantMessage, ResultMessage, TextBlock
from app.core.session_modes import chat_uploads_dir
from app.core.user_question_handler import DISMISSED_KEY, FLUID_ANSWER_KEY
from tests.core.engine_harness import collect
from tests.core.query_engine_flow_harness import (
    engine_patches,
    frames_of,
    fresh_adapter,
    scripted_events,
)

SID = "qe-perm-flow"
USER = "user-1"


def _ctx(tool_use_id="tu-1", **extra):
    return SimpleNamespace(tool_use_id=tool_use_id, agent_id=None, **extra)


def _allowed(result) -> bool:
    """What the adapter bridge decides: only this class name runs the tool."""
    return type(result).__name__ == "PermissionResultAllow"


@pytest.fixture(autouse=True)
def _clean_rules():
    permission_handler.clear_session_rules(SID)
    with patch("app.db.database.get_permission_rules", return_value=[]):
        yield
    permission_handler.clear_session_rules(SID)


async def _callback(tmp_path, *, overrides=None, session_row=None, profile_config=None,
                    user_id=USER, stack=None):
    """Run a turn up to the spawn refusal and return (callback, broadcast, bounds)."""
    # This callback fixture represents a fresh owner's per-message choice.
    # Producer/DB provenance is exercised in test_query_engine_turn_lifecycle.
    if session_row is None:
        # The owner's send stores its pick as the session's binding before it
        # queues the turn (``turn_manager._submit_blocking``).
        session_row = {"permission_mode": (overrides or {}).get("permission_mode") or "default"}
    if overrides:
        overrides = {**overrides, "_permission_explicit": True}
    broadcast = AsyncMock()
    own = stack is None
    stack = stack or ExitStack()
    try:
        bounds = engine_patches(
            stack,
            cwd=str(tmp_path / "missing-cwd-stops-before-connect"),
            session_row=session_row,
            profile={"id": "claude-code", "config": profile_config or {}},
        )
        events = await collect(qe._stream_to_websocket_impl(
            "hi", SID, "claude-code", overrides=overrides,
            broadcast_func=broadcast, user_id=user_id,
        ))
    finally:
        if own:
            stack.close()
    assert frames_of(events, "error")[0]["message"].startswith("Working directory does not exist")
    bounds.make_adapter.assert_not_called()
    assert callable(bounds.options.can_use_tool)
    return bounds.options.can_use_tool, broadcast, bounds


async def _next_prompt(broadcast, seen=0, timeout=15.0):
    """Wait for the (seen+1)th permission_request the origin socket receives.

    A prompt also goes to the connector funnel, whose first use imports the
    connector package (seconds, on the event loop). The app has it loaded from
    startup; the module import above does the same here so the first test does
    not spend its wait on that import.
    """
    async def _poll():
        while True:
            reqs = [c.args[0] for c in broadcast.await_args_list
                    if c.args and c.args[0].get("type") == "permission_request"]
            if len(reqs) > seen:
                return reqs[seen]
            await asyncio.sleep(0.005)
    return await asyncio.wait_for(_poll(), timeout)


def _prompt_count(broadcast):
    return sum(1 for c in broadcast.await_args_list
               if c.args and c.args[0].get("type") == "permission_request")


# ─── default mode: allow / deny / defer / remember ──────────────────────────


async def test_allow_answer_runs_the_tool_and_lights_then_clears_attention(tmp_path):
    cb, broadcast, bounds = await _callback(tmp_path)
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb("Bash", {"command": "ls"}, _ctx("tu-allow")))
        prompt = await _next_prompt(broadcast)
        assert prompt["tool_name"] == "Bash"
        assert prompt["tool_input"] == {"command": "ls"}
        assert prompt["tool_use_id"] == "tu-allow"
        assert prompt["request_id"].startswith(f"perm-{SID}-")
        res = await permission_handler.respond(prompt["request_id"], SID, "allow")
        assert res["resolved"] is True
        result = await asyncio.wait_for(call, 5)

    assert _allowed(result)
    assert result.updated_input == {"command": "ls"}
    counts = [c.args[2] for c in bounds.sync_engine.broadcast_session_attention.await_args_list]
    assert counts == [1, 0]
    kinds = {c.kwargs["kind"] for c in bounds.sync_engine.broadcast_session_attention.await_args_list}
    assert kinds == {"permission"}


async def test_deny_answer_blocks_the_tool(tmp_path):
    cb, broadcast, bounds = await _callback(tmp_path)
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb("Bash", {"command": "rm -rf /"}, _ctx()))
        prompt = await _next_prompt(broadcast)
        await permission_handler.respond(prompt["request_id"], SID, "deny")
        result = await asyncio.wait_for(call, 5)
    assert not _allowed(result)
    assert result.message == "User denied permission"


async def test_defer_answer_never_runs_the_tool(tmp_path):
    """A deferred prompt becomes a PreToolUse ``defer`` decision, which the
    adapter bridge reads as not-allowed (fail closed)."""
    cb, broadcast, bounds = await _callback(tmp_path)
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb("Bash", {"command": "make"}, _ctx()))
        prompt = await _next_prompt(broadcast)
        await permission_handler.respond(prompt["request_id"], SID, "defer")
        result = await asyncio.wait_for(call, 5)
    assert not _allowed(result)
    assert result["permissionDecision"] == "defer"
    assert result["hookEventName"] == "PreToolUse"


async def test_remember_for_session_skips_the_next_matching_prompt_only(tmp_path):
    cb, broadcast, bounds = await _callback(tmp_path)
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        first = asyncio.create_task(cb("Bash", {"command": "ls -la"}, _ctx("tu-a")))
        prompt = await _next_prompt(broadcast)
        await permission_handler.respond(
            prompt["request_id"], SID, "allow", remember="session", pattern="ls*",
        )
        assert _allowed(await asyncio.wait_for(first, 5))

        # Same tool + matching input: resolved by the remembered rule, no prompt.
        again = await asyncio.wait_for(cb("Bash", {"command": "ls /tmp"}, _ctx("tu-b")), 5)
        assert _allowed(again)
        assert _prompt_count(broadcast) == 1

        # Non-matching input still asks.
        other = asyncio.create_task(cb("Bash", {"command": "rm x"}, _ctx("tu-c")))
        prompt2 = await _next_prompt(broadcast, seen=1)
        assert prompt2["tool_input"] == {"command": "rm x"}
        other.cancel()
        with pytest.raises(asyncio.CancelledError):
            await other


async def test_cancelled_mid_prompt_clears_the_attention_indicator(tmp_path):
    """Interrupt while a tool waits on approval: the prompt's attention count
    is released even though no answer ever arrived."""
    cb, broadcast, bounds = await _callback(tmp_path)
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb("Write", {"file_path": "/x"}, _ctx()))
        await _next_prompt(broadcast)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    counts = [c.args[2] for c in bounds.sync_engine.broadcast_session_attention.await_args_list]
    assert counts == [1, 0]


async def test_no_user_id_prompts_without_attention_fanout(tmp_path):
    cb, broadcast, bounds = await _callback(tmp_path, user_id=None)
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb("Bash", {"command": "ls"}, _ctx()))
        prompt = await _next_prompt(broadcast)
        await permission_handler.respond(prompt["request_id"], SID, "allow")
        assert _allowed(await asyncio.wait_for(call, 5))
    bounds.sync_engine.broadcast_session_attention.assert_not_awaited()


async def test_context_enrichment_reaches_the_prompt(tmp_path):
    cb, broadcast, bounds = await _callback(tmp_path)
    ctx = _ctx("tu-e", decision_reason="outside cwd", blocked_path="/etc/passwd",
               title="Read a file", display_name="Read", description="read it")
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb("Read", {"file_path": "/etc/passwd"}, ctx))
        prompt = await _next_prompt(broadcast)
        await permission_handler.respond(prompt["request_id"], SID, "deny")
        await asyncio.wait_for(call, 5)
    assert prompt["decision_reason"] == "outside cwd"
    assert prompt["blocked_path"] == "/etc/passwd"
    assert prompt["title"] == "Read a file"
    assert prompt["display_name"] == "Read"
    assert prompt["description"] == "read it"


# ─── the permission-mode chain ──────────────────────────────────────────────


# The call is a file edit wherever Accept Edits is in the chain (the one class
# of tool it approves; any other tool would prompt under it exactly as under
# Ask and could not tell the two apart) and WebFetch elsewhere (on every lane's
# surface, including chat).
_EDIT = ("Write", {"file_path": "/tmp/x.txt", "content": "x"})
_FETCH = ("WebFetch", {"url": "https://example.com"})


@pytest.mark.parametrize("overrides,session_row,config,call,prompts", [
    # The owner's per-message pick, stored as the binding, beats the agent
    # config; a pick tighter than the current binding still wins.
    ({"permission_mode": "default"}, {"permission_mode": "bypassPermissions"},
     {"permission_mode": "acceptEdits"}, _EDIT, True),
    ({"permission_mode": "acceptEdits"}, {"permission_mode": "acceptEdits"}, {}, _EDIT, False),
    # Queued in Accept Edits, then the owner tightened the chat to Ask: the
    # later tightening wins over the pick the waiting message carries.
    ({"permission_mode": "acceptEdits"}, {"permission_mode": "default"}, {}, _EDIT, True),
    # No override: the session's binding wins over the agent config.
    (None, {"permission_mode": "bypassPermissions"}, {"permission_mode": "default"}, _FETCH, False),
    (None, {"permission_mode": "default"}, {"permission_mode": "bypassPermissions"}, _FETCH, True),
    # A real row with no binding (NULL): the session's agent mode decides.
    (None, {"id": SID, "permission_mode": None}, {"permission_mode": "acceptEdits"}, _EDIT, False),
    # Nothing anywhere: default.
    (None, None, {}, _FETCH, True),
    # The retired "plan" mode normalizes to default.
    ({"permission_mode": "plan"}, None, {}, _FETCH, True),
    # A chat session is pinned to default whatever the chain produced.
    (None, {"mode": "chat", "permission_mode": "bypassPermissions"}, {}, _FETCH, True),
], ids=[
    "override-default", "override-acceptEdits", "override-capped-by-later-tightening",
    "binding-bypass", "binding-default", "null-binding-uses-agent-acceptEdits", "nothing", "plan", "chat-pinned",
])
async def test_permission_mode_chain_decides_whether_a_tool_prompts(
    tmp_path, overrides, session_row, config, call, prompts,
):
    tool, tool_input = call
    cb, broadcast, bounds = await _callback(
        tmp_path, overrides=overrides, session_row=session_row, profile_config=config,
    )
    with patch.object(qe, "sync_engine", new=bounds.sync_engine):
        call = asyncio.create_task(cb(tool, tool_input, _ctx()))
        if prompts:
            prompt = await _next_prompt(broadcast)
            await permission_handler.respond(prompt["request_id"], SID, "deny")
            assert not _allowed(await asyncio.wait_for(call, 5))
        else:
            assert _allowed(await asyncio.wait_for(call, 5))
            assert _prompt_count(broadcast) == 0


# ─── agent tool restrictions and the chat fence ────────────────────────────


async def test_disallowed_tool_is_denied_without_prompting(tmp_path):
    cb, broadcast, _ = await _callback(
        tmp_path, overrides={"permission_mode": "bypassPermissions"},
        profile_config={"tool_selection_mode": "disallow", "disallowed_tools": ["mcp__browser__*"]},
    )
    result = await cb("mcp__browser__navigate", {"url": "x"}, _ctx())
    assert not _allowed(result)
    assert "disabled in this profile" in result.message
    assert _prompt_count(broadcast) == 0


async def test_tool_outside_the_allow_list_is_denied_without_prompting(tmp_path):
    cb, broadcast, _ = await _callback(
        tmp_path, overrides={"permission_mode": "bypassPermissions"},
        profile_config={"tool_selection_mode": "allow", "allowed_tools": ["Read"]},
    )
    denied = await cb("Bash", {"command": "ls"}, _ctx())
    assert not _allowed(denied)
    assert "not enabled in this profile" in denied.message
    assert _allowed(await cb("Read", {"file_path": "/x"}, _ctx()))
    assert _prompt_count(broadcast) == 0


async def test_chat_session_reads_only_its_uploads(tmp_path):
    cb, broadcast, _ = await _callback(tmp_path, session_row={"mode": "chat"})
    outside = await cb("Read", {"file_path": "/etc/passwd"}, _ctx())
    assert not _allowed(outside)
    assert "only read files attached to the chat" in outside.message

    inside = await cb("Read", {"file_path": str(chat_uploads_dir() / "a.txt")}, _ctx())
    assert _allowed(inside)
    assert _prompt_count(broadcast) == 0

    # A tool off the chat surface never reaches the prompt either.
    assert not _allowed(await cb("Bash", {"command": "ls"}, _ctx()))


# ─── computer use ──────────────────────────────────────────────────────────


async def test_computer_use_request_access_prompt_describes_the_apps(tmp_path):
    cb, _, _ = await _callback(tmp_path)
    route = AsyncMock(return_value=_DeferSentinel())
    with patch("app.core.provider_agent_approvals.route_permission", new=route):
        result = await cb("mcp__computer-use__request_access",
                          {"apps": ["Notepad", "Paint"], "reason": "draw"}, _ctx())
    kwargs = route.await_args.args[0]
    assert kwargs["title"] == "Computer control access"
    assert kwargs["description"] == "Claude wants to control: Notepad, Paint\nReason: draw"
    # Deferring access never grants it.
    assert not _allowed(result)
    assert result.message == "Computer-use access deferred by user"


async def test_computer_use_request_access_without_apps_names_the_screen(tmp_path):
    cb, _, _ = await _callback(tmp_path)
    route = AsyncMock(return_value=qe.PermissionResultAllow())
    with patch("app.core.provider_agent_approvals.route_permission", new=route):
        assert _allowed(await cb("mcp__computer-use__request_access", {}, _ctx()))
    assert route.await_args.args[0]["description"] == "Claude wants to control: your screen"


# ─── user questions ────────────────────────────────────────────────────────

_QUESTIONS = {"questions": [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]}


async def test_ask_user_question_posts_and_denies_so_the_turn_continues(tmp_path):
    cb, broadcast, bounds = await _callback(tmp_path)
    fake_uq = SimpleNamespace(post_question=AsyncMock(return_value={"posted": True}),
                              request_answers=AsyncMock())

    async def _post(**kwargs):
        await kwargs["fanout_func"]({"request_id": kwargs["request_id"]})
        return {"posted": True}

    fake_uq.post_question.side_effect = _post
    with patch.object(qe, "user_question_handler", new=fake_uq), \
            patch.object(qe, "sync_engine", new=bounds.sync_engine):
        result = await cb("AskUserQuestion", _QUESTIONS,
                          _ctx("tu-q", question_request_id="q-123"))
    kwargs = fake_uq.post_question.await_args.kwargs
    assert kwargs["request_id"] == "q-123"
    assert kwargs["tool_use_id"] == "tu-q"
    assert kwargs["session_id"] == SID
    assert kwargs["broadcast_func"] is broadcast
    assert not _allowed(result)
    assert "Question posted (request_id=q-123)" in result.message
    fake_uq.request_answers.assert_not_awaited()
    # The question fans out to every one of the user's devices.
    bounds.sync_engine.broadcast_user_question.assert_awaited_once_with(USER, {"request_id": "q-123"})


async def test_fluid_ask_user_blocks_for_the_answer_and_hands_it_to_the_tool(tmp_path):
    cb, _, bounds = await _callback(tmp_path)
    fake_uq = SimpleNamespace(post_question=AsyncMock(),
                              request_answers=AsyncMock(return_value={"Which?": "B"}))
    with patch.object(qe, "user_question_handler", new=fake_uq), \
            patch.object(qe, "sync_engine", new=bounds.sync_engine):
        result = await cb("AskUser", _QUESTIONS, _ctx("tu-f"))
    assert _allowed(result)
    assert result.updated_input["answers"] == {"Which?": "B"}
    assert result.updated_input[FLUID_ANSWER_KEY] == "B"
    assert fake_uq.request_answers.await_args.kwargs["request_id"].startswith(f"question-{SID}-")
    counts = [(c.args[2], c.kwargs["kind"])
              for c in bounds.sync_engine.broadcast_session_attention.await_args_list]
    assert counts == [(1, "question"), (0, "question")]


async def test_fluid_ask_user_dismissed_resolves_to_an_empty_answer(tmp_path):
    cb, _, bounds = await _callback(tmp_path)
    fake_uq = SimpleNamespace(post_question=AsyncMock(),
                              request_answers=AsyncMock(return_value={DISMISSED_KEY: True}))
    with patch.object(qe, "user_question_handler", new=fake_uq), \
            patch.object(qe, "sync_engine", new=bounds.sync_engine):
        result = await cb("AskUser", _QUESTIONS, _ctx())
    assert _allowed(result)
    assert result.updated_input[FLUID_ANSWER_KEY] == ""


async def test_fluid_ask_user_nonblocking_posts_and_merges_the_receipt(tmp_path):
    cb, _, bounds = await _callback(tmp_path)
    fake_uq = SimpleNamespace(post_question=AsyncMock(return_value={"_posted": "q-9"}),
                              request_answers=AsyncMock())
    with patch.object(qe, "user_question_handler", new=fake_uq), \
            patch.object(qe, "sync_engine", new=bounds.sync_engine):
        result = await cb("AskUser", _QUESTIONS, _ctx(nonblocking_question=True))
    assert _allowed(result)
    assert result.updated_input["_posted"] == "q-9"
    fake_uq.request_answers.assert_not_awaited()


async def test_question_tool_with_no_questions_is_allowed_untouched(tmp_path):
    cb, _, _ = await _callback(tmp_path)
    fake_uq = SimpleNamespace(post_question=AsyncMock(), request_answers=AsyncMock())
    with patch.object(qe, "user_question_handler", new=fake_uq):
        result = await cb("AskUserQuestion", {}, _ctx())
    assert _allowed(result)
    assert result.updated_input == {}
    fake_uq.post_question.assert_not_awaited()


# ─── the callback invoked mid-stream by the adapter ─────────────────────────


async def test_prompt_raised_mid_stream_is_answered_and_the_turn_completes(tmp_path):
    decisions = []
    broadcast = AsyncMock()
    with ExitStack() as stack:
        adapter = fresh_adapter()
        bounds = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)

        async def _tool_call():
            call = asyncio.create_task(bounds.options.can_use_tool(
                "Bash", {"command": "pytest"}, _ctx("tu-mid")))
            prompt = await _next_prompt(broadcast)
            await permission_handler.respond(prompt["request_id"], SID, "allow")
            decisions.append(await asyncio.wait_for(call, 5))

        adapter.receive_events = scripted_events([
            _tool_call,
            AssistantMessage(content=[TextBlock(text="ran it")], model="m"),
            ResultMessage(subtype="success"),
        ])
        events = await collect(qe._stream_to_websocket_impl(
            "run the tests", SID, "claude-code", broadcast_func=broadcast, user_id=USER,
        ))

    assert [_allowed(d) for d in decisions] == [True]
    assert frames_of(events, "chunk") == [{"type": "chunk", "content": "ran it"}]
    assert frames_of(events, "done")
    # The pooled client keeps the callback but drops this turn's socket.
    assert bounds.pool.state.callback_refs["broadcast_func"] is None
    assert bounds.pool.state.adapter is adapter
