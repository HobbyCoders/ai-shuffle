"""Tests for the v2 rewind → event-log resolution (app/api/commands.py).

Covers ``_resolve_rewind_from_seq`` (backfilled-uuid short-circuit,
content+occurrence resolution including duplicate prompts, include_response,
fail-closed paths), ``_resolve_checkpoint_uuid_from_content`` (the
target_content/target_occurrence affordance), and the ``execute_rewind``
integration: refusal happens BEFORE anything is truncated, and a successful
chat rewind appends an unfenced ``transcript.rewound``.

Everything is driven directly (no ASGITransport); the event log rides the
autouse SQLite shim.
"""

import json
from types import SimpleNamespace

import pytest

import app.api.commands as cmd
from app.api.commands import (
    RewindRequestV2,
    _is_prompt_event,
    _load_message_events,
    _resolve_checkpoint_uuid_from_content,
    _resolve_rewind_from_seq,
    execute_rewind,
)
from app.core.jsonl_rewind import Checkpoint, RewindResult
from app.db import session_events as se

SID = "sess-rewind"
SDK = "sdk-rewind"
WD = "/tmp/wd"


def _cp(uuid, text, role="user", index=0):
    return Checkpoint(
        uuid=uuid, index=index, message_preview=text[:100],
        full_message=text, role=role,
    )


def _user_ev(content, uuid=None, kind=None):
    payload = {"content": content}
    if uuid:
        payload["uuid"] = uuid
    if kind:
        payload["kind"] = kind
    return {"type": "message.user", "turn_id": "backfill", "payload": payload}


def _asst_ev(content, uuid=None):
    payload = {"content": content, "kind": "text"}
    if uuid:
        payload["uuid"] = uuid
    return {"type": "message.assistant", "turn_id": "backfill", "payload": payload}


def _patch_checkpoints(monkeypatch, checkpoints):
    monkeypatch.setattr(
        cmd.jsonl_rewind_service, "get_checkpoints",
        lambda sdk_session_id, working_dir: list(checkpoints),
    )


# ---------------------------------------------------------------------------
# _is_prompt_event — checkpoint candidacy
# ---------------------------------------------------------------------------

def test_is_prompt_event_normal_user_message():
    row = {"event_type": "message.user", "payload": {"content": "hello there"}}
    assert _is_prompt_event(row) is True


def test_is_prompt_event_excludes_task_notification():
    row = {
        "event_type": "message.user",
        "payload": {
            "content": (
                "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
                "<task-notification>\n<detail>done</detail>\n</task-notification>"
            ),
        },
    }
    assert _is_prompt_event(row) is False


# ---------------------------------------------------------------------------
# _resolve_rewind_from_seq — uuid short-circuit
# ---------------------------------------------------------------------------

async def test_backfilled_uuid_short_circuit(mock_db, monkeypatch):
    se.append_events(SID, [
        _user_ev("first", uuid="u-1"),
        _asst_ev("reply one", uuid="a-1"),
        _user_ev("second", uuid="u-2"),
        _asst_ev("reply two", uuid="a-2"),
    ])
    _patch_checkpoints(monkeypatch, [])  # must NOT be needed

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-2", False, "user")
    assert err is None
    assert from_seq == 3  # the u-2 event itself


async def test_include_response_uses_next_user_prompt(mock_db, monkeypatch):
    se.append_events(SID, [
        _user_ev("first", uuid="u-1"),
        _asst_ev("reply one"),
        _user_ev("second", uuid="u-2"),
    ])
    _patch_checkpoints(monkeypatch, [])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-1", True, "user")
    assert err is None
    assert from_seq == 3  # next message.user after the target


async def test_include_response_at_tail_is_noop(mock_db, monkeypatch):
    se.append_events(SID, [
        _user_ev("only", uuid="u-1"),
        _asst_ev("reply"),
    ])
    _patch_checkpoints(monkeypatch, [])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-1", True, "user")
    assert err is None
    assert from_seq is None  # nothing after the response — no event to append


async def test_assistant_target_resolves_by_uuid(mock_db, monkeypatch):
    se.append_events(SID, [
        _user_ev("first", uuid="u-1"),
        _asst_ev("reply one", uuid="a-1"),
        _user_ev("second", uuid="u-2"),
    ])
    _patch_checkpoints(monkeypatch, [])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "a-1", False, "assistant")
    assert err is None
    assert from_seq == 3  # keep the assistant msg, drop from the next prompt


# ---------------------------------------------------------------------------
# _resolve_rewind_from_seq — content + occurrence
# ---------------------------------------------------------------------------

async def test_content_resolution_maps_to_matching_prompt(mock_db, monkeypatch):
    # Live v2 events: no uuids stamped.
    se.append_events(SID, [
        {"type": "turn.started"},
        _user_ev("first prompt"),
        _asst_ev("reply one"),
        {"type": "turn.started"},
        _user_ev("second prompt"),
    ])
    _patch_checkpoints(monkeypatch, [
        _cp("u-1", "first prompt", index=0),
        _cp("u-2", "second prompt", index=1),
    ])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-2", False, "user")
    assert err is None
    assert from_seq == 5


async def test_duplicate_content_disambiguated_by_occurrence(mock_db, monkeypatch):
    # Two identical prompts — occurrence among same-text checkpoints picks
    # the right one on each side.
    se.append_events(SID, [
        _user_ev("do it again"),
        _asst_ev("done"),
        _user_ev("do it again"),
        _asst_ev("done again"),
    ])
    _patch_checkpoints(monkeypatch, [
        _cp("u-1", "do it again", index=0),
        _cp("u-2", "do it again", index=1),
    ])

    first, err1 = _resolve_rewind_from_seq(SID, SDK, WD, "u-1", False, "user")
    second, err2 = _resolve_rewind_from_seq(SID, SDK, WD, "u-2", False, "user")
    assert (err1, err2) == (None, None)
    assert first == 1
    assert second == 3


async def test_command_prompts_do_not_shift_resolution(mock_db, monkeypatch):
    # A backfilled command prompt does not appear in the checkpoint list and
    # must not shift resolution.
    se.append_events(SID, [
        _user_ev("real prompt", kind="user"),
        _user_ev("/compact", kind="command"),
        _user_ev("second real", kind="user"),
    ])
    _patch_checkpoints(monkeypatch, [
        _cp("u-1", "real prompt", index=0),
        _cp("u-2", "second real", index=1),
    ])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-2", False, "user")
    assert err is None
    assert from_seq == 3


# ---------------------------------------------------------------------------
# _resolve_rewind_from_seq — fail closed
# ---------------------------------------------------------------------------

async def test_fail_closed_on_content_mismatch(mock_db, monkeypatch):
    se.append_events(SID, [_user_ev("what the log recorded")])
    _patch_checkpoints(monkeypatch, [_cp("u-1", "what the jsonl says")])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-1", False, "user")
    assert from_seq is None
    assert err is not None and "content guard" in err


async def test_fail_closed_when_target_not_in_checkpoints(mock_db, monkeypatch):
    se.append_events(SID, [_user_ev("hello")])
    _patch_checkpoints(monkeypatch, [_cp("u-1", "hello")])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-nope", False, "user")
    assert from_seq is None
    assert err is not None


async def test_fail_closed_when_log_has_fewer_prompts(mock_db, monkeypatch):
    se.append_events(SID, [_user_ev("only one")])
    _patch_checkpoints(monkeypatch, [
        _cp("u-1", "only one", index=0),
        _cp("u-2", "a second the log never saw", index=1),
    ])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-2", False, "user")
    assert from_seq is None
    assert err is not None


async def test_fail_closed_on_empty_log(mock_db, monkeypatch):
    _patch_checkpoints(monkeypatch, [_cp("u-1", "hello")])
    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "u-1", False, "user")
    assert from_seq is None
    assert err is not None


async def test_fail_closed_assistant_target_without_uuid_match(mock_db, monkeypatch):
    se.append_events(SID, [_user_ev("hello"), _asst_ev("reply")])
    _patch_checkpoints(monkeypatch, [_cp("a-1", "reply", role="assistant")])

    from_seq, err = _resolve_rewind_from_seq(SID, SDK, WD, "a-1", False, "assistant")
    assert from_seq is None
    assert err is not None and "assistant" in err


# ---------------------------------------------------------------------------
# _resolve_checkpoint_uuid_from_content (target_content affordance)
# ---------------------------------------------------------------------------

async def test_content_affordance_resolves_uuid(mock_db, monkeypatch):
    _patch_checkpoints(monkeypatch, [
        _cp("u-1", "first"), _cp("a-1", "reply", role="assistant"),
        _cp("u-2", "second"),
    ])
    uuid, err = _resolve_checkpoint_uuid_from_content(SDK, WD, "second", 0)
    assert err is None
    assert uuid == "u-2"


async def test_content_affordance_skips_synthetic_interrupt_lines(mock_db, monkeypatch):
    # Regression: the SDK transcript records a synthetic user line after every
    # interrupt. A global user-prompt ordinal skews past it (the old scheme
    # refused any rewind in an interrupted session); content anchoring must
    # resolve straight through.
    _patch_checkpoints(monkeypatch, [
        _cp("u-1", "first"),
        _cp("u-int", "[Request interrupted by user]"),
        _cp("u-2", "second"),
        _cp("u-int2", "[Request interrupted by user]"),
        _cp("u-3", "third"),
    ])
    uuid, err = _resolve_checkpoint_uuid_from_content(SDK, WD, "third", 0)
    assert err is None
    assert uuid == "u-3"


async def test_content_affordance_duplicate_content_uses_occurrence(mock_db, monkeypatch):
    _patch_checkpoints(monkeypatch, [
        _cp("u-1", "do it again"),
        _cp("u-2", "do it again"),
    ])
    uuid0, err0 = _resolve_checkpoint_uuid_from_content(SDK, WD, "do it again", 0)
    uuid1, err1 = _resolve_checkpoint_uuid_from_content(SDK, WD, "do it again", 1)
    assert (err0, err1) == (None, None)
    assert (uuid0, uuid1) == ("u-1", "u-2")


async def test_content_affordance_fails_closed_on_no_match(mock_db, monkeypatch):
    _patch_checkpoints(monkeypatch, [_cp("u-1", "actual prompt")])
    uuid, err = _resolve_checkpoint_uuid_from_content(SDK, WD, "stale prompt", 0)
    assert uuid is None
    assert "content guard" in (err or "")


async def test_content_affordance_occurrence_out_of_range(mock_db, monkeypatch):
    _patch_checkpoints(monkeypatch, [_cp("u-1", "only")])
    uuid, err = _resolve_checkpoint_uuid_from_content(SDK, WD, "only", 5)
    assert uuid is None
    assert "content guard" in (err or "")


async def test_content_affordance_empty_content_refused(mock_db, monkeypatch):
    _patch_checkpoints(monkeypatch, [_cp("u-1", "only")])
    uuid, err = _resolve_checkpoint_uuid_from_content(SDK, WD, "   ", 0)
    assert uuid is None
    assert err is not None


# ---------------------------------------------------------------------------
# _load_message_events — transcript.rewound filtering
# ---------------------------------------------------------------------------

async def test_load_message_events_drops_rewound_rows(mock_db):
    # Rows logically erased by a later transcript.rewound must not take part
    # in resolution — they mirror messages the client no longer renders.
    se.append_events(SID, [
        _user_ev("kept"),                                     # seq 1
        _asst_ev("kept reply"),                               # seq 2
        _user_ev("discarded prompt"),                         # seq 3
        {"type": "transcript.rewound", "payload": {"from_seq": 3}},  # seq 4
        _user_ev("after"),                                    # seq 5
    ])
    rows = _load_message_events(SID)
    contents = [(r.get("payload") or {}).get("content") for r in rows]
    assert contents == ["kept", "kept reply", "after"]


# ---------------------------------------------------------------------------
# execute_rewind integration (v2 session)
# ---------------------------------------------------------------------------

@pytest.fixture
def rewind_env(mock_db, monkeypatch):
    """Stub every collaborator of execute_rewind except the event log."""
    state = SimpleNamespace(
        truncate_calls=[], evicted=[], update_calls=[],
        remaining_user_turns=None,
        session_row={
            "id": SID, "sdk_session_id": SDK, "project_id": None,
            "v2_stream": True, "turn_count": 2,
        },
    )

    monkeypatch.setattr(cmd, "database", SimpleNamespace(
        get_session=lambda sid: dict(state.session_row),
        update_session=lambda *a, **k: state.update_calls.append((a, k)),
        get_session_messages=lambda sid: [],
    ))
    monkeypatch.setattr(cmd, "get_working_dir_for_project", lambda pid, worktree_id=None: WD)

    class _Pool:
        def contains(self, sid):
            return False
        async def remove(self, sid):
            state.evicted.append(sid)
    monkeypatch.setattr(cmd, "get_session_pool", lambda: _Pool())

    def _truncate(sdk_session_id, target_uuid, working_dir, include_response, target_role):
        state.truncate_calls.append(target_uuid)
        return RewindResult(
            success=True, message="ok", checkpoint_uuid=target_uuid,
            messages_removed=2, remaining_user_turns=state.remaining_user_turns,
        )
    monkeypatch.setattr(cmd.jsonl_rewind_service, "truncate_to_checkpoint", _truncate)

    # The Postgres session store is always truncated first (postgres_only is
    # the one and only mode); stub it to succeed, and the emptiness probe to
    # report a still-populated transcript.
    async def _store_truncate(**kw):
        return 2
    monkeypatch.setattr(cmd, "truncate_session_store_to_checkpoint", _store_truncate)

    async def _store_empty(sdk_session_id):
        return False
    monkeypatch.setattr(cmd, "main_transcript_is_empty", _store_empty)

    monkeypatch.setattr(cmd, "resolve_rewind_anchor", lambda **kw: 7)
    monkeypatch.setattr(cmd, "apply_rewind_delete", lambda **kw: 2)
    monkeypatch.setattr(cmd, "invalidate_parse_cache", lambda sdk: None)

    # (V2: the live session store is deleted — execute_rewind's only live-state
    # teardown is the session-pool eviction stubbed above.)

    async def _broadcast(**kw):
        return None
    monkeypatch.setattr(
        cmd, "sync_engine", SimpleNamespace(broadcast_session_rewound=_broadcast)
    )
    return state


async def test_execute_rewind_appends_transcript_rewound(rewind_env, monkeypatch):
    se.append_events(SID, [
        _user_ev("first", uuid="u-1"),
        _asst_ev("reply one"),
        _user_ev("second", uuid="u-2"),
    ])
    _patch_checkpoints(monkeypatch, [])

    resp = await execute_rewind(
        SID, RewindRequestV2(target_uuid="u-2", include_response=False)
    )
    assert resp.success is True
    assert resp.chat_rewound is True
    assert rewind_env.truncate_calls == ["u-2"]

    rows = se.get_events(SID)
    assert rows[-1]["event_type"] == "transcript.rewound"
    assert rows[-1]["payload"]["from_seq"] == 3
    assert rows[-1]["run_id"] is None  # unfenced


async def test_full_rewind_clears_sdk_session_pointer(rewind_env, monkeypatch):
    # Rewinding to the very first message empties the JSONL. Resuming the old
    # sdk_session_id then fails ("No conversation found"), bricking the
    # session — the pointer must be cleared so the next message starts fresh.
    rewind_env.remaining_user_turns = 0
    se.append_events(SID, [_user_ev("first", uuid="u-1")])
    _patch_checkpoints(monkeypatch, [])

    resp = await execute_rewind(
        SID, RewindRequestV2(target_uuid="u-1", include_response=False)
    )
    assert resp.success is True
    assert any(
        k.get("sdk_session_id") == "" for _, k in rewind_env.update_calls
    ), f"sdk_session_id was not cleared: {rewind_env.update_calls}"


async def test_partial_rewind_keeps_sdk_session_pointer(rewind_env, monkeypatch):
    # A rewind that leaves user turns behind must NOT touch the resume pointer.
    rewind_env.remaining_user_turns = 1
    se.append_events(SID, [
        _user_ev("first", uuid="u-1"),
        _asst_ev("reply"),
        _user_ev("second", uuid="u-2"),
    ])
    _patch_checkpoints(monkeypatch, [])

    resp = await execute_rewind(
        SID, RewindRequestV2(target_uuid="u-2", include_response=False)
    )
    assert resp.success is True
    assert not any(
        k.get("sdk_session_id") == "" for _, k in rewind_env.update_calls
    )


async def test_execute_rewind_refuses_before_truncating(rewind_env, monkeypatch):
    se.append_events(SID, [_user_ev("recorded content")])
    _patch_checkpoints(monkeypatch, [_cp("u-1", "different content")])

    resp = await execute_rewind(
        SID, RewindRequestV2(target_uuid="u-1", include_response=False)
    )
    assert resp.success is False
    assert "content guard" in (resp.error or "")
    assert rewind_env.truncate_calls == []  # NOTHING was truncated
    assert all(
        r["event_type"] != "transcript.rewound" for r in se.get_events(SID)
    )


async def test_execute_rewind_content_payload_resolves_uuid(rewind_env, monkeypatch):
    se.append_events(SID, [
        _user_ev("first", uuid="u-1"),
        _user_ev("second", uuid="u-2"),
    ])
    _patch_checkpoints(monkeypatch, [
        _cp("u-1", "first", index=0),
        _cp("u-2", "second", index=1),
    ])

    resp = await execute_rewind(SID, RewindRequestV2(
        target_uuid="", target_occurrence=0, target_content="second",
        include_response=False,
    ))
    assert resp.success is True
    assert rewind_env.truncate_calls == ["u-2"]
    rows = se.get_events(SID)
    assert rows[-1]["event_type"] == "transcript.rewound"
    assert rows[-1]["payload"]["from_seq"] == 2


async def test_execute_rewind_content_guard_refuses(rewind_env, monkeypatch):
    se.append_events(SID, [_user_ev("first", uuid="u-1")])
    _patch_checkpoints(monkeypatch, [_cp("u-1", "first", index=0)])

    resp = await execute_rewind(SID, RewindRequestV2(
        target_uuid="", target_occurrence=0, target_content="not what is there",
        include_response=False,
    ))
    assert resp.success is False
    assert rewind_env.truncate_calls == []


async def test_execute_steer_rewind_refuses_before_any_mutation(rewind_env, monkeypatch):
    steer = _user_ev("mid-turn only")
    steer["payload"]["checkpoint_backed"] = False
    se.append_events(SID, [_user_ev("normal"), steer])
    _patch_checkpoints(monkeypatch, [_cp("u-1", "normal")])
    before = se.get_events(SID)
    resp = await execute_rewind(SID, RewindRequestV2(
        target_content="mid-turn only", target_occurrence=0, include_response=False,
    ))
    assert resp.success is False
    assert "0 user prompt" in resp.error
    assert rewind_env.truncate_calls == []
    assert rewind_env.evicted == []
    assert se.get_events(SID) == before


async def test_execute_rewind_missing_target_refused(rewind_env, monkeypatch):
    _patch_checkpoints(monkeypatch, [])
    resp = await execute_rewind(SID, RewindRequestV2(target_uuid=""))
    assert resp.success is False
    assert rewind_env.truncate_calls == []


async def test_execute_rewind_non_v2_session_unchanged(rewind_env, monkeypatch):
    # Non-v2 sessions must keep the legacy behavior: no resolution, no event.
    rewind_env.session_row["v2_stream"] = False
    _patch_checkpoints(monkeypatch, [])

    resp = await execute_rewind(
        SID, RewindRequestV2(target_uuid="u-1", include_response=False)
    )
    assert resp.success is True
    assert rewind_env.truncate_calls == ["u-1"]
    assert se.get_events(SID) == []  # no transcript.rewound appended


async def _produce_failed_attachment_turns(sdk_session_id, tmp_path):
    """Persist real NativeLoop user nodes, including a rejected image turn."""
    import asyncio

    from app.core.environment import generate_turn_environment_note
    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.turn_recorder import TurnRecorder
    from app.db import database
    from fluid.engine.native import NativeLoop
    from fluid.events import UserTurn
    from fluid.messages import Image, Text
    from fluid.providers.base import ProviderCaps
    from fluid.tools.registry import ToolCtx

    class RejectImageProvider:
        name = "openai_responses"
        caps = ProviderCaps()

        async def stream(self, request):
            raise ValueError("Invalid image data")
            yield  # make this a streaming provider, without contacting a service

    database.create_profile("rewind-test", "Rewind test", None, {})
    database.create_session(SID, "rewind-test", derive_provider_from_profile=False)
    store = PgFluidStore()
    loop = NativeLoop(
        provider=RejectImageProvider(), store=store, session_id=sdk_session_id,
        model="test-model", ctx=ToolCtx(cwd=str(tmp_path)), setting_sources=[],
    )
    adapter = HarnessAdapter()
    adapter._engine = loop
    # Use the host's real context-note producer and send_query enrichment, not
    # a fixture that pre-populates the cleaned checkpoint text we want to read.
    adapter._env_note = generate_turn_environment_note
    prompt = "Inspect @/uploads/map.webp"
    try:
        for index in range(2):
            if index:
                # Delivered steers are recorded as user rows/events but not
                # standalone Fluid prompts; this matches the live failure.
                steer = "Please preserve the map"
                recorder = TurnRecorder(SID, None, None, None, "test")
                await recorder.record_steered_user_message(steer)
                database.add_session_message(SID, "user", steer)
            recorder = TurnRecorder(SID, None, None, None, "test")
            await recorder.start(prompt)
            database.add_session_message(SID, "user", prompt)
            if index == 0:
                await adapter.send_query(prompt)
            else:
                # Native multimodal UserTurn shape: image + typed text, with
                # the same host enrichment; failure leaves this node at head.
                await loop.submit(UserTurn(content=[
                    Image(media_type="image/webp", data="invalid-image"),
                    Text(prompt + "\n\n" + adapter._turn_context_note()),
                ]))
            await asyncio.wait_for(loop.wait_turn(), timeout=10)
            await recorder.on_event({"type": "error", "message": "Invalid image data"})
        events = await store.events_since(sdk_session_id, 0)
        assert sum(type(event).__name__ == "ErrorEvent" for event in events) == 2
        path = await store.load_path(sdk_session_id)
        assert len(path) == 2
        assert generate_turn_environment_note() in path[-1].message.text()
        assert any(isinstance(block, Image) for block in path[-1].message.content)
        return prompt, [node.node_id for node in path]
    finally:
        await loop.aclose()


async def test_failed_enriched_attachment_turn_rewinds_all_stores(
    rewind_env, monkeypatch, tmp_path,
):
    from app.core import rewind_db_sync
    from app.core.provider.fluid_transcript import load_fluid_path
    from app.db import database

    sdk = "ses_failed_attachment"
    rewind_env.session_row["sdk_session_id"] = sdk
    prompt, nodes = await _produce_failed_attachment_turns(sdk, tmp_path)
    monkeypatch.setattr(cmd, "resolve_rewind_anchor", rewind_db_sync.resolve_rewind_anchor)
    monkeypatch.setattr(cmd, "apply_rewind_delete", rewind_db_sync.apply_rewind_delete)

    # No broad prefix match: an adjacent different prompt still fails closed.
    uuid_, error = _resolve_checkpoint_uuid_from_content(sdk, WD, "Inspect", 0)
    assert uuid_ is None and "content guard" in error
    assert _resolve_checkpoint_uuid_from_content(sdk, WD, prompt, 1) == (nodes[1], None)

    response = await execute_rewind(SID, RewindRequestV2(
        target_content=prompt, target_occurrence=1, include_response=False,
    ))
    assert response.success is True
    assert response.chat_rewound is True
    assert [node["node_id"] for node in load_fluid_path(sdk)] == nodes[:1]
    rows = database.get_session_messages(SID)
    assert [row["content"] for row in rows] == [prompt, "Please preserve the map"]
    assert rows[0]["jsonl_uuid"] == nodes[0]
    assert [row["payload"]["content"] for row in _load_message_events(SID)] == [
        prompt, "Please preserve the map",
    ]
    assert se.get_events(SID)[-1]["payload"]["from_seq"] == 6

    # Rewinding the first unanswered prompt empties Fluid but preserves its
    # resumable session identity, unlike the legacy JSONL full-reset path.
    response = await execute_rewind(SID, RewindRequestV2(
        target_content=prompt, target_occurrence=0, include_response=False,
    ))
    assert response.success is True
    assert load_fluid_path(sdk) == []
    assert database.get_session_messages(SID) == []
    assert _load_message_events(SID) == []
    assert not any(kw.get("sdk_session_id", "unchanged") is None
                   for _, kw in rewind_env.update_calls)

async def _produce_checkpoint_and_steer_turns(session_id, sdk_session_id, tmp_path):
    """NativeLoop delivery -> runtime receipt -> TurnRecorder persistence.

    The next standalone prompt equals the intervening steer, guarding against
    resolving an ordinary checkpoint to a message with no boundary of its own.
    """
    import asyncio

    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.core.session_runtime import SessionRuntime
    from app.core.turn_recorder import TurnRecorder
    from app.db import database, turns as turns_db
    from fluid.engine.native import NativeLoop
    from fluid.events.model import SteerDelivered, TextDelta, ToolResult, ToolUse, TurnComplete
    from fluid.events.ops import Steer, UserTurn
    from fluid.permissions.engine import PermissionEngine
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.tools.inproc import create_inproc_server, tool
    from fluid.tools.registry import ToolCtx, ToolRegistry

    prompt, steer = "Start a tool", "Continue normally"
    database.create_profile("checkpoint-test", "Checkpoint test", None, {}, if_absent=True)
    database.create_session(session_id, "checkpoint-test", derive_provider_from_profile=False)
    database.update_session(session_id, sdk_session_id=sdk_session_id, v2_stream=True)
    live = turns_db.enqueue_turn(session_id, "user", prompt=prompt)
    turns_db.start_turn(live["id"])
    live = turns_db.get_turn(live["id"])
    queued = turns_db.enqueue_turn(session_id, "user", prompt=steer, command_id="steer-command")
    runtime = SessionRuntime(live)

    class Provider:
        name = "openai_responses"
        caps = ProviderCaps()
        calls = 0

        async def stream(self, request):
            self.calls += 1
            yield StreamChunk("message_start", {})
            if self.calls == 1:
                yield StreamChunk("block_start", {
                    "index": 0, "block_type": "tool_use", "tool_name": "mcp__test__interject",
                    "tool_use_id": "tool-interject",
                })
                yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": "{}"})
            else:
                yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
                yield StreamChunk("text_delta", {"index": 0, "text": "Finished normally"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {}, "stop_reason": "tool_use" if self.calls == 1 else "end_turn"})
            yield StreamChunk("message_stop", {})

    @tool("interject", "Deliver the pending user message during tool execution", {"type": "object"})
    async def interject(args):
        await loop.submit(Steer(text=steer, id=queued["id"]))
        return {"content": [{"type": "text", "text": "tool completed"}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("test", [interject]))
    store = PgFluidStore()
    loop = NativeLoop(
        provider=Provider(), store=store, session_id=sdk_session_id, model="test-model",
        registry=registry, permissions=PermissionEngine("never_ask"),
        ctx=ToolCtx(cwd=str(tmp_path)), setting_sources=[],
    )
    recorder = TurnRecorder(session_id, None, live["id"], None, "test")

    async def record_events(events):
        for event in events:
            if isinstance(event, ToolUse):
                await recorder.on_event({"type": "tool_use", "id": event.tool_use_id,
                                         "name": event.name, "input": event.input})
            elif isinstance(event, ToolResult):
                await recorder.on_event({"type": "tool_result", "tool_use_id": event.tool_use_id,
                                         "output": event.output})
            elif isinstance(event, SteerDelivered):
                await runtime._on_steer_delivered(recorder, {"type": "steer_delivered", "steers": event.steers})
            elif isinstance(event, TextDelta):
                await recorder.on_event({"type": "stream_delta", "delta_type": "text", "content": event.text})
            elif isinstance(event, TurnComplete):
                await recorder.on_event({"type": "done", "metadata": {"stop_reason": event.stop_reason}})

    try:
        await recorder.start(prompt)
        database.add_session_message(session_id, "user", prompt)
        await loop.submit(UserTurn(content=prompt))
        await asyncio.wait_for(loop.wait_turn(), 10)
        events = await store.events_since(sdk_session_id, 0)
        result = next(e for e in events if isinstance(e, ToolResult))
        receipt = next(e for e in events if isinstance(e, SteerDelivered))
        assert receipt.seq > result.seq
        assert steer in result.output
        await record_events(events)
        await recorder.close()
        turns_db.settle_turn(live["id"], "completed")
        absorbed = turns_db.get_turn(queued["id"])
        assert absorbed["state"] == "completed"
        assert absorbed["stop_reason"] == "steered"

        recorder = TurnRecorder(session_id, None, "next-normal-turn", None, "test")
        await recorder.start(steer)
        database.add_session_message(session_id, "user", steer)
        await loop.submit(UserTurn(content=steer))
        await asyncio.wait_for(loop.wait_turn(), 10)
        await record_events(await store.events_since(sdk_session_id, events[-1].seq))
        return prompt, steer
    finally:
        await recorder.close()
        await loop.aclose()


async def test_native_steer_reload_has_no_checkpoint_but_normal_prompt_resolves(mock_db, tmp_path):
    from app.core.history_backfill import _convert_one
    from app.core.provider.fluid_transcript import fluid_live_messages, get_fluid_checkpoints

    sdk = "ses_checkpoint_steer"
    prompt, steer = await _produce_checkpoint_and_steer_turns(SID, sdk, tmp_path)
    rows = se.get_events(SID)
    users = [r for r in rows if r["event_type"] == "message.user"]
    assert [r["payload"]["content"] for r in users] == [prompt, steer, steer]
    assert users[1]["payload"]["checkpoint_backed"] is False
    assert users[1]["turn_id"] == users[0]["turn_id"]
    assert [r["seq"] for r in _load_message_events(SID) if _is_prompt_event(r)] == [users[0]["seq"], users[2]["seq"]]

    checkpoints = [cp for cp in get_fluid_checkpoints(sdk) if cp.role == "user"]
    assert [cp.full_message for cp in checkpoints] == [prompt, steer]
    target, error = _resolve_checkpoint_uuid_from_content(sdk, str(tmp_path), steer, 0)
    assert error is None and target == checkpoints[1].uuid
    from_seq, error = _resolve_rewind_from_seq(SID, sdk, str(tmp_path), target, False, "user")
    assert error is None and from_seq == users[2]["seq"]
    # A non-existent steer occurrence refuses rather than guessing a cut.
    assert _resolve_checkpoint_uuid_from_content(sdk, str(tmp_path), steer, 1)[0] is None
    # The reload shows the steer where the event log has it, without making
    # it a checkpoint; a backfill from this read keeps that marking.
    messages, _ = fluid_live_messages(sdk, str(tmp_path))
    shown = [m for m in messages if m["role"] == "user"]
    assert [m["content"] for m in shown] == [prompt, steer, steer]
    assert [m["checkpoint_backed"] for m in shown] == [True, False, True]
    assert [_convert_one(SID, m)[0]["payload"]["checkpoint_backed"] for m in shown] == [True, False, True]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("include_response", [None, True, False])
async def test_native_steered_turn_rewind_keeps_native_and_replay_aligned(
    mock_db, monkeypatch, tmp_path, legacy, include_response,
):
    from app.core.provider.fluid_transcript import (
        fluid_live_messages, fluid_transcript_entries, get_fluid_checkpoints, load_fluid_path,
    )
    from app.db import database
    from app.db.base import get_db

    sdk = "ses_native_steer_rewind"
    prompt, steer = await _produce_checkpoint_and_steer_turns(SID, sdk, tmp_path)
    if legacy:
        # A producer-built old log has no optional capability field; the real
        # turn identity, duplicate text and native checkpoints stay untouched.
        with get_db() as conn:
            rows = se.get_events(SID)
            for row in rows:
                if row["event_type"] == "message.user":
                    payload = dict(row["payload"])
                    payload.pop("checkpoint_backed", None)
                    conn.cursor().execute(
                        "UPDATE session_events SET payload = %s WHERE session_id = %s AND seq = %s",
                        (json.dumps(payload), SID, row["seq"]),
                    )
    users = [row for row in se.get_events(SID) if row["event_type"] == "message.user"]
    assert [row["payload"]["content"] for row in users] == [prompt, steer, steer]
    checkpoints = [cp for cp in get_fluid_checkpoints(sdk) if cp.role == "user"]
    target = checkpoints[0].uuid
    request_fields = {"target_uuid": target}
    if include_response is not None:
        request_fields["include_response"] = include_response
    request = RewindRequestV2(**request_fields)
    assert request.include_response is (include_response is not False)
    expected_cut = users[2]["seq"] if request.include_response else users[0]["seq"]
    assert _resolve_rewind_from_seq(SID, sdk, str(tmp_path), target, request.include_response, "user") == (expected_cut, None)
    monkeypatch.setattr(cmd, "get_working_dir_for_project", lambda *a, **kw: str(tmp_path))
    class Pool:
        def contains(self, sid):
            return False
    monkeypatch.setattr(cmd, "get_session_pool", lambda: Pool())

    response = await execute_rewind(SID, request)
    assert response.success is True and response.chat_rewound is True
    rewind = se.get_events(SID)[-1]
    assert rewind["event_type"] == "transcript.rewound"
    assert rewind["payload"]["from_seq"] == expected_cut
    assert database.get_session(SID)["sdk_session_id"] == sdk
    native, _ = fluid_live_messages(sdk, str(tmp_path))
    replay = _load_message_events(SID)
    if not request.include_response:
        assert native == [] and load_fluid_path(sdk) == [] and replay == []
    else:
        assert [m["content"] for m in native if m["role"] == "user"] == [prompt, steer]
        assert [r["payload"]["content"] for r in replay if r["event_type"] == "message.user"] == [prompt, steer]
        native_replies = [m["content"] for m in native if m["role"] == "assistant" and m.get("type") == "text"]
        # The native producer streams assistant.text_delta, not closed
        # message.assistant rows. Replay the retained stream payloads too;
        # _load_message_events only indexes closed rows for rewind resolution.
        kept_events = [row for row in se.get_events(SID) if row["seq"] < expected_cut]
        replay_text = "".join(
            row["payload"]["text"] for row in kept_events
            if row["event_type"] == "assistant.text_delta"
        )
        assert native_replies == [replay_text] == ["Finished normally"]
        # Rendering folds tool results into their tool-use card; inspect the
        # authoritative retained native node payload for the delivered steer.
        results = [
            block
            for entry in fluid_transcript_entries(sdk)
            for block in entry["message"].get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        assert len(results) == 1 and steer in str(results[0]["content"])
        assert all(r["seq"] < expected_cut for r in replay)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("boundary_kind", [None, "command"])
async def test_keep_response_skips_steers_but_retains_real_legacy_boundaries(
    mock_db, monkeypatch, legacy, boundary_kind,
):
    first, interjection = _user_ev("again"), _user_ev("again")
    first["turn_id"] = interjection["turn_id"] = "live-turn"
    if not legacy:
        interjection["payload"]["checkpoint_backed"] = False
    following = _user_ev("again", kind=boundary_kind)
    if boundary_kind == "command":
        # Commands are real turn boundaries even though they aren't prompt
        # checkpoint candidates. Preserve the existing command behavior.
        following["payload"]["checkpoint_backed"] = False
    se.append_events(SID, [first, interjection, _asst_ev("reply"), following])
    _patch_checkpoints(monkeypatch, [_cp("u-1", "again"), _cp("u-2", "again")])
    assert _resolve_rewind_from_seq(SID, SDK, WD, "u-1", True, "user") == (4, None)
    assert _resolve_rewind_from_seq(SID, SDK, WD, "u-1", False, "user") == (1, None)


@pytest.mark.parametrize("legacy", [False, True])
async def test_keep_response_tail_steer_is_not_a_next_prompt(mock_db, monkeypatch, legacy):
    first, steer = _user_ev("first"), _user_ev("mid-turn")
    first["turn_id"] = steer["turn_id"] = "live-turn"
    if not legacy:
        steer["payload"]["checkpoint_backed"] = False
    se.append_events(SID, [first, steer, _asst_ev("reply")])
    _patch_checkpoints(monkeypatch, [_cp("u-1", "first")])
    assert _resolve_rewind_from_seq(SID, SDK, WD, "u-1", True, "user") == (None, None)
    assert _resolve_rewind_from_seq(SID, SDK, WD, "u-1", False, "user") == (1, None)


@pytest.mark.parametrize("legacy", [False, True])
async def test_assistant_checkpoint_boundary_skips_same_turn_steer(mock_db, monkeypatch, legacy):
    first, steer = _user_ev("first"), _user_ev("mid-turn")
    first["turn_id"] = steer["turn_id"] = "live-turn"
    if not legacy:
        steer["payload"]["checkpoint_backed"] = False
    se.append_events(SID, [
        first, _asst_ev("partial response", uuid="a-1"), steer,
        _asst_ev("finished response"), _user_ev("next"),
    ])
    _patch_checkpoints(monkeypatch, [])
    assert _resolve_rewind_from_seq(SID, SDK, WD, "a-1", True, "assistant") == (5, None)
    assert _resolve_rewind_from_seq(SID, SDK, WD, "a-1", False, "assistant") == (5, None)


async def test_legacy_same_turn_steer_does_not_shift_normal_checkpoint(mock_db, monkeypatch):
    first, steer, following = _user_ev("again"), _user_ev("again"), _user_ev("again")
    first["turn_id"] = steer["turn_id"] = "real-turn"
    following["turn_id"] = "next-turn"
    se.append_events(SID, [first, steer, following])
    _patch_checkpoints(monkeypatch, [_cp("first", "again"), _cp("next", "again")])
    rows = _load_message_events(SID)
    assert [_is_prompt_event(row) for row in rows] == [True, False, True]
    assert _resolve_rewind_from_seq(SID, SDK, WD, "next", False, "user") == (3, None)
    # Normal legacy events sharing the backfill sentinel are not inferred steers.
    se.append_events(SID, [_user_ev("backfilled"), _user_ev("backfilled")])
    assert all(_is_prompt_event(row) for row in _load_message_events(SID)[-2:])

@pytest.mark.parametrize("target_index", [0, 1])
@pytest.mark.parametrize("include_response", [None, True, False])
async def test_real_standalone_peer_rewind_native_and_logical_boundaries(
    mock_db, monkeypatch, tmp_path, target_index, include_response,
):
    import asyncio
    from app.core import session_orchestration, turn_scheduler
    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.core.provider.fluid_transcript import fluid_live_messages, get_fluid_checkpoints
    from app.core.turn_recorder import TurnRecorder
    from app.db import database, sessions, turns
    from fluid.engine.native import NativeLoop
    from fluid.events.ops import UserTurn
    from fluid.events import TextDelta, TurnComplete
    from fluid.permissions.engine import PermissionEngine
    from fluid.providers.base import ProviderCaps, StreamChunk
    from fluid.tools.registry import ToolCtx, ToolRegistry

    sid, caller, sdk, profile = "standalone-peer", "standalone-sender", "ses_standalone_peer", "peer-test"
    database.create_profile(profile, "Peer test", None, {}, if_absent=True)
    for session in (sid, caller):
        sessions.create_session(session, profile, is_active=True, model="test-model", permission_mode="default")
    database.update_session(sid, sdk_session_id=sdk, v2_stream=True)
    origin = turns.enqueue_turn(caller, "user", prompt="Coordinate the next turn", profile_id=profile)
    assert turns.start_turn(origin["id"])
    assert turns.settle_turn(origin["id"], "completed")
    monkeypatch.setattr(turn_scheduler, "kick", lambda session: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda session: None)

    class Provider:
        name = "openai_responses"
        caps = ProviderCaps()
        calls = 0
        async def stream(self, request):
            self.calls += 1
            yield StreamChunk("message_start", {})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": f"Reply {self.calls}"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"})
            yield StreamChunk("message_stop", {})

    store = PgFluidStore()
    loop = NativeLoop(provider=Provider(), store=store, session_id=sdk, model="test-model",
                      registry=ToolRegistry(), permissions=PermissionEngine("never_ask"),
                      ctx=ToolCtx(cwd=str(tmp_path)), setting_sources=[])
    prompts, row_ids, cursor = [], [], 0

    async def run(row):
        nonlocal cursor
        assert turns.start_turn(row["id"])
        recorder = TurnRecorder(sid, row["command_id"], row["id"], None, "test")
        recorder.turn_id = row["id"]
        await recorder.start(row["prompt"], profile_id=profile, source=row["source"])
        database.add_session_message(sid, "user", row["prompt"])
        await loop.submit(UserTurn(content=row["prompt"]))
        await asyncio.wait_for(loop.wait_turn(), 10)
        events = await store.events_since(sdk, cursor)
        for event in events:
            if isinstance(event, TextDelta):
                await recorder.on_event({"type": "stream_delta", "delta_type": "text", "content": event.text})
            elif isinstance(event, TurnComplete):
                await recorder.on_event({"type": "done", "metadata": {"stop_reason": event.stop_reason}})
        cursor = events[-1].seq
        await recorder.close()
        assert turns.settle_turn(row["id"], "completed")
        prompts.append(row["prompt"])
        row_ids.append(row["id"])

    try:
        await run(turns.enqueue_turn(sid, "user", prompt="First normal A", profile_id=profile))
        outcome = await session_orchestration.make_handler(lambda: caller)(
            "message", {"session_id": sid, "message": "Standalone peer Q", "request_id": "standalone-q"}, ToolCtx())
        assert not outcome.is_error, outcome.text
        peer = turns.get_turn(json.loads(outcome.text)["turn_id"])
        assert peer["source"] == "agent_message" and peer["state"] == "queued"
        await run(peer)
        await run(turns.enqueue_turn(sid, "user", prompt="Next normal C", profile_id=profile))
    finally:
        await loop.aclose()

    users = [row for row in se.get_events(sid) if row["event_type"] == "message.user"]
    checkpoints = [cp for cp in get_fluid_checkpoints(sdk) if cp.role == "user"]
    assert [row["turn_id"] for row in users] == row_ids
    assert [cp.full_message for cp in checkpoints] == prompts
    assert users[1]["payload"].get("checkpoint_backed") is not False  # Real standalone SDK boundary.
    assert _resolve_checkpoint_uuid_from_content(sdk, str(tmp_path), prompts[1], 0) == (checkpoints[1].uuid, None)
    request_fields = {"target_uuid": checkpoints[target_index].uuid}
    if include_response is not None:
        request_fields["include_response"] = include_response
    request = RewindRequestV2(**request_fields)
    retained = target_index + (1 if request.include_response else 0)
    cut = users[retained]["seq"]
    assert _resolve_rewind_from_seq(sid, sdk, str(tmp_path), request.target_uuid,
                                    request.include_response, "user") == (cut, None)
    monkeypatch.setattr(cmd, "get_working_dir_for_project", lambda *a, **kw: str(tmp_path))
    class Pool:
        def contains(self, session):
            return False
    monkeypatch.setattr(cmd, "get_session_pool", lambda: Pool())
    result = await execute_rewind(sid, request)
    assert result.success and result.chat_rewound
    assert se.get_events(sid)[-1]["payload"]["from_seq"] == cut
    native, _ = fluid_live_messages(sdk, str(tmp_path))
    replay = _load_message_events(sid)
    assert [m["content"] for m in native if m["role"] == "user"] == prompts[:retained]
    assert [r["payload"]["content"] for r in replay if r["event_type"] == "message.user"] == prompts[:retained]
    assert [m["content"] for m in native if m["role"] == "assistant"] == [f"Reply {i+1}" for i in range(retained)]
    kept = [row for row in se.get_events(sid) if row["seq"] < cut]
    assert "".join(row["payload"]["text"] for row in kept if row["event_type"] == "assistant.text_delta") == "".join(f"Reply {i+1}" for i in range(retained))
