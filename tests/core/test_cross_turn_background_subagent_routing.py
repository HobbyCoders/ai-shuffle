"""Cross-turn background sub-agent StreamEvent routing (prod-readiness).

A background sub-agent (Task/Agent launched with run_in_background=true) OUTLIVES
the turn that launched it. Its StreamEvents / AssistantMessages continue to arrive
on the pooled SDK client in LATER, unrelated turns — still carrying
``parent_tool_use_id`` set to the launching tool_use block's id.

The streaming loop routes sub-agent events by testing ``parent_tool_id in
task_tool_uses``. ``task_tool_uses`` is PER-TURN local state, populated only when
the launching Agent/Task tool_use block streamed in THAT turn. So in a follow-up
turn the fresh empty dict does not know the background id, the gate FAILS, and the
sub-agent's events fall through to the MAIN-agent handlers:

* a sub-agent text_delta accumulates into the parent's main text lane, and
* a sub-agent content_block_start tool_use is emitted as a top-level ``tool_use``,
  appended to the parent's live buffer / persisted as an orphan row, and (via the
  block_start) finalizes the parent's in-flight text block early / mid-word.

The fix seeds ``task_tool_uses`` at turn start from
``task_registry.get_background_launches`` (a durable set only cleared with the
session), so every gate recognises cross-turn background ids again.

Harness mirrors tests/core/test_busy_write_guard_engine.py: no Postgres / real SDK.
"""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import app.core.query_engine as qe
from app.core.session_pool import SessionState
from app.core.task_registry import task_registry

from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import StreamEvent


# ─── harness (mirrors test_busy_write_guard_engine._fake_state / _chat_patches) ─


def _fake_state(messages, **overrides) -> SessionState:
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.sdk_client = MagicMock()
    adapter.sdk_client.query = AsyncMock()

    async def _response():
        for m in messages:
            yield m

    adapter.sdk_client.receive_response = MagicMock(side_effect=lambda: _response())
    state = SessionState(adapter=adapter, is_connected=True)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


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

    async def remove_broken(self, session_id):
        self.state = None


def _chat_patches(stack: ExitStack, pool: _StubPool):
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "claude-code", "config": {}}
    ))

    async def _fake_run_sync(fn, *a, **k):
        return {"id": "row-1"}

    stack.enter_context(patch.object(qe, "run_sync", new=AsyncMock(side_effect=_fake_run_sync)))
    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock()
    stack.enter_context(patch.object(qe, "direct_oauth", new=oauth))
    options = MagicMock()
    options.include_partial_messages = True  # exercise the StreamEvent path
    options.cwd = None
    stack.enter_context(patch.object(
        qe, "build_options_from_profile", return_value=(options, {})
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qe, "credentials_rotated", return_value=False))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    stack.enter_context(patch.object(qe, "resolve_note_references", side_effect=lambda p: p))
    stack.enter_context(patch.object(qe, "_persist_turn_results", new=AsyncMock()))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    sync = MagicMock()
    sync.broadcast_message_added = AsyncMock()
    sync.broadcast_stream_start = AsyncMock()
    sync.broadcast_stream_chunk = AsyncMock()
    sync.broadcast_stream_end = AsyncMock()
    stack.enter_context(patch.object(qe, "sync_engine", new=sync))
    return options


async def _collect(agen):
    events = []
    async for ev in agen:
        events.append(ev)
    return events


def _stream_text_delta(parent_tool_use_id, text):
    return StreamEvent(
        uuid="u-sd",
        session_id="sess-x",
        parent_tool_use_id=parent_tool_use_id,
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def _stream_block_start_tool_use(parent_tool_use_id, tool_id, name):
    return StreamEvent(
        uuid="u-bs",
        session_id="sess-x",
        parent_tool_use_id=parent_tool_use_id,
        event={
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": tool_id, "name": name},
        },
    )


BG_TOOL_ID = "toolu_bg_from_prior_turn"


class TestCrossTurnBackgroundSubagentRouting:
    def setup_method(self):
        task_registry.reset_all()

    def teardown_method(self):
        task_registry.reset_all()

    async def test_cross_turn_subagent_stream_events_route_as_subagent_chunk(self):
        """A background sub-agent launched in a PRIOR turn (recorded in the registry)
        must have this turn's StreamEvents routed as ``subagent_chunk`` — NEVER as
        main-lane ``stream_delta`` text, and its block_start tool_use must NOT emit a
        top-level ``tool_use`` / interrupt the parent's main text lane."""
        session_id = "sess-cross-turn"

        # Simulate the PRIOR turn: the parent launched a background sub-agent.
        task_registry.mark_background_launch(session_id, BG_TOOL_ID)

        # THIS turn's stream: a genuine parent text delta, then the background
        # sub-agent's leaked text delta + tool_use block_start (parent_tool_use_id
        # points at the prior-turn launch id).
        messages = [
            _stream_text_delta(None, "parent says hi"),
            _stream_text_delta(BG_TOOL_ID, "SUBAGENT internal step"),
            _stream_block_start_tool_use(BG_TOOL_ID, "toolu_child", "Bash"),
            # A sub-agent AssistantMessage tool_use too (the AssistantMessage lane).
            AssistantMessage(
                content=[ToolUseBlock(id="toolu_child2", name="Read", input={"file": "x"})],
                model="claude",
                parent_tool_use_id=BG_TOOL_ID,
            ),
        ]

        state = _fake_state(messages)
        pool = _StubPool(state)

        with ExitStack() as stack:
            _chat_patches(stack, pool)
            events = await _collect(
                qe.stream_to_websocket("follow-up prompt", session_id, "claude-code")
            )

        types = [e.get("type") for e in events]

        # The parent's own text delta still flows on the main lane.
        assert any(
            e.get("type") == "stream_delta"
            and e.get("delta_type") == "text"
            and e.get("content") == "parent says hi"
            for e in events
        ), f"parent main-lane text delta missing: {types}"

        # The sub-agent's text delta is routed as subagent_chunk, NOT stream_delta.
        assert {
            "type": "subagent_chunk",
            "agent_id": BG_TOOL_ID,
            "content": "SUBAGENT internal step",
        } in events, f"sub-agent text not routed as subagent_chunk: {events}"

        assert not any(
            e.get("type") == "stream_delta" and "SUBAGENT" in (e.get("content") or "")
            for e in events
        ), f"sub-agent text LEAKED into main-lane stream_delta: {events}"

        # The sub-agent's block_start tool_use must NOT surface as a main-lane
        # stream_block_start (that early-finalizes the parent's text block).
        assert not any(
            e.get("type") == "stream_block_start" for e in events
        ), f"sub-agent block_start LEAKED as main-lane stream_block_start: {types}"

        # The sub-agent's AssistantMessage tool_use routes as subagent_tool_use,
        # never as a top-level tool_use appended to the parent's buffer.
        assert any(
            e.get("type") == "subagent_tool_use" and e.get("agent_id") == BG_TOOL_ID
            for e in events
        ), f"sub-agent tool_use not routed as subagent_tool_use: {types}"
        assert not any(
            e.get("type") == "tool_use" for e in events
        ), f"sub-agent tool_use LEAKED as a top-level tool_use: {events}"
