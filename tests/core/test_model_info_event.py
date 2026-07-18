"""Pump-level tests for the live ``model_info`` system note.

``_stream_to_websocket_impl`` emits ``{"type": "system", "subtype":
"model_info", "data": {"model": <actual>, "requested_model":
<options.model|None>}}`` so the frontend can see which model is ACTUALLY
running a turn (vs. what was requested) and flag model rerouting. The rules
under test:

- the SDK ``init`` frame emits one note when it carries a ``model`` and seeds
  the turn-local dedup value;
- each MAIN-lane ``AssistantMessage`` re-emits only when ``message.model``
  CHANGES from the last emitted value;
- a subagent-lane ``AssistantMessage`` (parent_tool_use_id set) never emits.

These drive the real streaming generator DIRECTLY over a faked SDK client and
collect the yielded frames (no SSE endpoint — that buffers infinite bodies and
deadlocks pytest). The harness mirrors tests/core/test_incremental_turn_persistence.py
but stubs turn-end persistence (not under test here).
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import uuid

from claude_agent_sdk import (
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
    adapter.sdk_client = MagicMock()
    adapter.sdk_client.query = AsyncMock()
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

    async def remove_broken(self, session_id):
        self.state = None


def _engine_patches(stack: ExitStack, pool: _StubPool, cwd: str, model=None, provider=None):
    """Patch the subprocess/profile boundaries; stub turn-end persistence.

    Returns the options MagicMock so the test can set ``options.model`` (the
    ``requested_model`` passthrough source). ``provider`` seeds the profile
    config's ``provider`` (the resolved-provider source for the model_info
    frame; None = native Anthropic).
    """
    profile_config = {} if provider is None else {"provider": provider}
    stack.enter_context(patch.object(
        qe, "get_profile", return_value={"id": "default", "config": profile_config}
    ))
    oauth = MagicMock()
    oauth.ensure_fresh_credentials = AsyncMock()
    stack.enter_context(patch.object(qe, "direct_oauth", new=oauth))
    options = MagicMock()
    options.include_partial_messages = False
    options.cwd = cwd
    options.model = model
    stack.enter_context(patch(
        "app.core.sdk_options.build_options_from_profile",
        return_value=(options, {}),
    ))
    stack.enter_context(patch.object(qe, "get_session_pool", return_value=pool))
    stack.enter_context(patch.object(qe, "is_subprocess_alive", return_value=True))
    stack.enter_context(patch.object(qe, "requires_reconnect", return_value=False))
    stack.enter_context(patch.object(qe, "credentials_rotated", return_value=False))
    stack.enter_context(patch.object(qe, "mark_subprocess_oom_killable", new=MagicMock()))
    stack.enter_context(patch.object(qe, "resolve_note_references", side_effect=lambda p: p))
    stack.enter_context(patch.object(qe, "push_memory", new=AsyncMock()))
    # Turn-end persistence is not under test — neutralise it.
    stack.enter_context(patch.object(qe, "_persist_turn_results", new=AsyncMock()))
    return options


def _stream_response(messages):
    """Build a receive_response factory that yields the given SDK messages."""

    async def _gen():
        for m in messages:
            yield m

    return MagicMock(side_effect=lambda: _gen())


async def _collect(agen):
    events = []
    async for ev in agen:
        events.append(ev)
    return events


def _new_session(profile_id="default"):
    session_id = f"sess-{uuid.uuid4()}"
    database.create_session(session_id=session_id, profile_id=profile_id)
    return session_id


async def _run(messages, model=None, tmp_path=None, provider=None):
    """Drive the streaming loop over ``messages`` and return model_info frames."""
    session_id = _new_session()
    state = _make_state()
    pool = _StubPool(state)
    state.adapter.sdk_client.receive_response = _stream_response(messages)
    with ExitStack() as stack:
        _engine_patches(stack, pool, str(tmp_path), model=model, provider=provider)
        events = await _collect(qe.stream_to_websocket("hi", session_id, "default"))
    return [
        e for e in events
        if e.get("type") == "system" and e.get("subtype") == "model_info"
    ]


def _init(sdk_sid, model=None):
    data = {"session_id": sdk_sid}
    if model is not None:
        data["model"] = model
    return SystemMessage(subtype="init", data=data)


def _asst(model, uuid_="a-1", parent=None):
    return AssistantMessage(
        content=[TextBlock(text="hello")],
        model=model,
        uuid=uuid_,
        parent_tool_use_id=parent,
    )


# ─── tests ───────────────────────────────────────────────────────────────────


async def test_assistant_message_emits_single_model_info(mock_db, tmp_path):
    # (a) one AssistantMessage with model X → exactly one model_info note whose
    # data.model == X and requested_model is the options.model passthrough.
    frames = await _run(
        [_init("sdk-1"), _asst("claude-actual")],
        model="claude-requested",
        tmp_path=tmp_path,
    )
    assert len(frames) == 1
    assert frames[0] == {
        "type": "system",
        "subtype": "model_info",
        "data": {
            "model": "claude-actual",
            "requested_model": "claude-requested",
            # Native session → provider is nested under data and is None (R1).
            "provider": None,
        },
    }


async def test_second_same_model_no_duplicate(mock_db, tmp_path):
    # (b) a second AssistantMessage with the SAME model emits no duplicate.
    frames = await _run(
        [_init("sdk-1"), _asst("claude-x", "a-1"), _asst("claude-x", "a-2")],
        model="claude-req",
        tmp_path=tmp_path,
    )
    assert len(frames) == 1
    assert frames[0]["data"]["model"] == "claude-x"


async def test_model_change_mid_turn_emits_second(mock_db, tmp_path):
    # (c) a model CHANGE mid-turn emits a second note.
    frames = await _run(
        [_init("sdk-1"), _asst("claude-x", "a-1"), _asst("claude-y", "a-2")],
        model="claude-req",
        tmp_path=tmp_path,
    )
    assert [f["data"]["model"] for f in frames] == ["claude-x", "claude-y"]


async def test_init_model_seeds_and_dedupes_first_assistant(mock_db, tmp_path):
    # init carrying a model emits once and SEEDS the dedup value, so an
    # identical first AssistantMessage does not re-emit.
    frames = await _run(
        [_init("sdk-1", model="claude-x"), _asst("claude-x")],
        model="claude-req",
        tmp_path=tmp_path,
    )
    assert len(frames) == 1
    assert frames[0]["data"]["model"] == "claude-x"


async def test_init_model_then_assistant_change_emits_twice(mock_db, tmp_path):
    # init model X + AssistantMessage model Y → two notes (X seeded, Y is a change).
    frames = await _run(
        [_init("sdk-1", model="claude-x"), _asst("claude-y")],
        model="claude-req",
        tmp_path=tmp_path,
    )
    assert [f["data"]["model"] for f in frames] == ["claude-x", "claude-y"]


async def test_requested_model_null_when_options_model_unset(mock_db, tmp_path):
    # options.model None → requested_model passthrough is None.
    frames = await _run(
        [_init("sdk-1"), _asst("claude-x")],
        model=None,
        tmp_path=tmp_path,
    )
    assert len(frames) == 1
    assert frames[0]["data"]["requested_model"] is None


async def test_provider_none_for_native_session(mock_db, tmp_path):
    # R1 contract: the frame's `data` always carries a `provider` key; for a
    # native Anthropic session (no profile/override provider) it is None.
    frames = await _run(
        [_init("sdk-1"), _asst("claude-x")],
        model="claude-req",
        tmp_path=tmp_path,
    )
    assert len(frames) == 1
    assert "provider" in frames[0]["data"]
    assert frames[0]["data"]["provider"] is None


async def test_provider_present_for_gateway_session(mock_db, tmp_path):
    # R1 contract: a provider session (profile config carries `provider`) nests
    # the resolved provider id under `data.provider` so the frontend can
    # suppress the reroute pill (a gateway model never maps to a Claude family).
    frames = await _run(
        [_init("sdk-1"), _asst("openai/gpt-5.6")],
        model="openai/gpt-5.6",
        provider="prov-openrouter",
        tmp_path=tmp_path,
    )
    assert len(frames) == 1
    assert frames[0]["data"]["provider"] == "prov-openrouter"
    assert frames[0]["data"]["model"] == "openai/gpt-5.6"


async def test_subagent_lane_assistant_never_emits(mock_db, tmp_path):
    # A subagent-lane AssistantMessage (parent_tool_use_id set) with a different
    # model must NOT emit — model_info tracks the MAIN turn's model only.
    frames = await _run(
        [
            _init("sdk-1"),
            _asst("claude-main", "a-1"),
            _asst("claude-subagent", "a-2", parent="tu-task"),
        ],
        model="claude-req",
        tmp_path=tmp_path,
    )
    assert [f["data"]["model"] for f in frames] == ["claude-main"]
