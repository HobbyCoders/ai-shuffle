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
