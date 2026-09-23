"""A subagent's token usage must never be attributed to the parent session.

Every ``AssistantMessage.usage`` block used to overwrite
``metadata["last_call_tokens_in" | "last_call_cache_creation_tokens" |
"last_call_cache_read_tokens"]`` and yield a ``turn_usage`` frame — with no
sidechain gate. Because ``_persist_turn_results`` writes those metadata keys
into ``sessions.last_input_tokens`` (and friends), a Task/Agent subagent's
much smaller context measurement landed on the PARENT row and the context-usage
pill collapsed mid-turn.

The adjacent ``model_info`` note already gated on the sidechain flag
(``parent_tool_use_id`` is not None); the usage blocks now do the same.

Harness mirrors tests/core/test_model_info_event.py: the real streaming
generator is driven directly over a faked pooled adapter and the yielded
frames are collected (no SSE endpoint — that buffers infinite bodies and
deadlocks pytest).
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.provider.events import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
)

import app.core.query_engine as qe
from app.core.session_pool import SessionState
from app.db import database


# ─── minimal engine harness (subprocess/profile boundaries only) ─────────────


def _make_state():
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=True)
    adapter.send_query = AsyncMock()
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


def _engine_patches(stack: ExitStack, pool: _StubPool, cwd: str) -> AsyncMock:
    """Patch the subprocess/profile boundaries; capture the turn-end persist."""
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "default", "config": {}}
    ))
    options = MagicMock()
    options.include_partial_messages = False
    options.cwd = cwd
    options.model = None
    options.aih_provider_id = None
    stack.enter_context(patch(
        "app.core.sdk_options.build_options_from_profile",
        return_value=(options, {}),
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    stack.enter_context(patch.object(qe, "resolve_note_references", side_effect=lambda p: p))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    persist = AsyncMock()
    stack.enter_context(patch.object(qe, "_persist_turn_results", new=persist))
    return persist


def _stream_response(messages):
    async def _gen():
        for m in messages:
            yield m

    return MagicMock(side_effect=lambda: _gen())


def _new_session(profile_id="default"):
    session_id = f"sess-{uuid.uuid4()}"
    database.create_session(session_id=session_id, profile_id=profile_id)
    return session_id


def _init(sdk_sid="sdk-1"):
    return SystemMessage(subtype="init", data={"session_id": sdk_sid})


def _asst(usage, uuid_="a-1", parent=None):
    return AssistantMessage(
        content=[TextBlock(text="hello")],
        model="claude-x",
        uuid=uuid_,
        parent_tool_use_id=parent,
        usage=usage,
    )


MAIN_USAGE = {
    "input_tokens": 120_000,
    "cache_creation_input_tokens": 4_000,
    "cache_read_input_tokens": 90_000,
}
SUBAGENT_USAGE = {
    "input_tokens": 900,
    "cache_creation_input_tokens": 10,
    "cache_read_input_tokens": 20,
}


async def _run(messages, tmp_path):
    """Drive the streaming loop; return (yielded events, persist mock)."""
    session_id = _new_session()
    state = _make_state()
    pool = _StubPool(state)
    state.adapter.receive_events = _stream_response(messages)
    events = []
    with ExitStack() as stack:
        persist = _engine_patches(stack, pool, str(tmp_path))
        async for ev in qe.stream_to_websocket("hi", session_id, "default"):
            events.append(ev)
    return events, persist


# ─── tests ───────────────────────────────────────────────────────────────────


async def test_sidechain_usage_does_not_change_last_call_tokens(mock_db, tmp_path):
    """A subagent AssistantMessage arriving AFTER the parent's must not
    overwrite the parent's last-call figures, nor yield a turn_usage frame."""
    events, persist = await _run(
        [
            _init(),
            _asst(MAIN_USAGE, "a-1"),
            _asst(SUBAGENT_USAGE, "a-2", parent="toolu_task_1"),
        ],
        tmp_path,
    )

    metadata = persist.await_args.kwargs["metadata"]
    assert metadata["last_call_tokens_in"] == 120_000
    assert metadata["last_call_cache_creation_tokens"] == 4_000
    assert metadata["last_call_cache_read_tokens"] == 90_000

    usage_frames = [e for e in events if e.get("type") == "turn_usage"]
    assert len(usage_frames) == 1
    assert usage_frames[0]["usage"] == MAIN_USAGE


async def test_sidechain_only_turn_records_no_last_call_tokens(mock_db, tmp_path):
    """With ONLY subagent usage in the stream, nothing is recorded at all —
    the parent keeps whatever its previous turn measured."""
    events, persist = await _run(
        [_init(), _asst(SUBAGENT_USAGE, "a-1", parent="toolu_task_1")],
        tmp_path,
    )

    metadata = persist.await_args.kwargs["metadata"]
    assert "last_call_tokens_in" not in metadata
    assert "last_call_cache_creation_tokens" not in metadata
    assert "last_call_cache_read_tokens" not in metadata
    assert [e for e in events if e.get("type") == "turn_usage"] == []


async def test_main_lane_usage_still_recorded(mock_db, tmp_path):
    """Control: the gate must not suppress ordinary main-lane usage."""
    events, persist = await _run([_init(), _asst(MAIN_USAGE, "a-1")], tmp_path)

    metadata = persist.await_args.kwargs["metadata"]
    assert metadata["last_call_tokens_in"] == 120_000
    assert [e["usage"] for e in events if e.get("type") == "turn_usage"] == [MAIN_USAGE]
