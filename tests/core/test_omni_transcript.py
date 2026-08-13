"""omni transcript/checkpoint facade — the read+rewind+fork path for flag-on sessions.

Seeds ``omni_sessions`` / ``omni_session_nodes`` rows directly against the
pg_shim, then drives the facade the app's rewind, fork and /live paths now call.

The fixtures are written as raw ``Message.to_dict()``-shaped JSON rather than
built through omni's dataclasses on purpose: the facade reads the store with
plain SQL and never imports ``omni``, so neither should its tests (CI installs
the app without the harness). ``test_fixture_matches_real_omni_serialization``
pins the hand-written shape against the real serializer wherever omni IS
importable, so the fixtures cannot silently drift from what the harness writes.
"""

from __future__ import annotations

import pytest

from app.core.provider.omni_transcript import (
    fork_omni_session,
    get_omni_checkpoints,
    is_omni_session,
    load_omni_path,
    new_omni_session_id,
    omni_live_messages,
    omni_transcript_entries,
    rewind_omni_session,
)


def _msg(role, blocks):
    return {"role": role, "content": blocks, "provider_meta": {}}


def _user(text):
    return _msg("user", [{"kind": "text", "text": text}])


def _assistant(text):
    return _msg("assistant", [{"kind": "text", "text": text}])


def _insert(session_id, nodes):
    """Write a session and its nodes exactly as PgOmniStore would."""
    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO omni_sessions (session_id, head_node_id, created_at) "
            "VALUES (%s, %s, %s)",
            (session_id, nodes[-1][0], 1000.0),
        )
        for i, (node_id, parent_id, message) in enumerate(nodes):
            cursor.execute(
                "INSERT INTO omni_session_nodes "
                "(session_id, node_id, parent_id, ts, message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (session_id, node_id, parent_id, 1000.0 + i, Json(message)),
            )


def _seed(session_id="ses_fixture"):
    """A two-turn conversation with a tool call in the middle.

    n1 user "first prompt"
    n2 assistant "first reply"
    n3 assistant tool_use (Read)
    n4 user tool_result      <- NOT a checkpoint (tool echo, not a human turn)
    n5 assistant "after tool"
    n6 user "second prompt"
    n7 assistant "second reply"
    """
    _insert(session_id, [
        ("n1", None, _user("first prompt")),
        ("n2", "n1", _assistant("first reply")),
        ("n3", "n2", _msg("assistant", [{
            "kind": "tooluseblock", "id": "t1", "name": "Read",
            "input": {"file_path": "/x"},
        }])),
        ("n4", "n3", _msg("user", [{
            "kind": "toolresultblock", "tool_use_id": "t1",
            "content": [{"kind": "text", "text": "file body"}],
            "is_error": False,
        }])),
        ("n5", "n4", _assistant("after tool")),
        ("n6", "n5", _user("second prompt")),
        ("n7", "n6", _assistant("second reply")),
    ])
    return session_id


def test_fixture_matches_real_omni_serialization():
    """The hand-written fixture JSON must equal what omni actually persists."""
    pytest.importorskip("omni")
    from omni.messages.content import Message, Text, ToolResultBlock, ToolUseBlock

    assert Message(role="user", content=[Text(text="hi")]).to_dict() == _user("hi")
    assert Message(
        role="assistant",
        content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "/x"})],
    ).to_dict() == _msg("assistant", [{
        "kind": "tooluseblock", "id": "t1", "name": "Read",
        "input": {"file_path": "/x"},
    }])
    assert Message(
        role="user",
        content=[ToolResultBlock(tool_use_id="t1", content=[Text(text="file body")])],
    ).to_dict() == _msg("user", [{
        "kind": "toolresultblock", "tool_use_id": "t1",
        "content": [{"kind": "text", "text": "file body"}],
        "is_error": False,
    }])


def _head(session_id):
    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "SELECT head_node_id FROM omni_sessions WHERE session_id = %s",
            (session_id,),
        )
        return dict(cursor.fetchone())["head_node_id"]


# ─── session detection ───────────────────────────────────────────────────────

def test_legacy_session_id_is_never_omni(mock_db):
    # A CLI session id is a UUID — the prefix gate rejects it without a query,
    # which is what keeps the flag-off path byte-identical.
    assert is_omni_session("6f1c9e2a-6f0f-4a5e-9a4f-2b6c0d5e7a11") is False
    assert is_omni_session(None) is False
    assert is_omni_session("") is False


def test_omni_shaped_id_must_still_exist(mock_db):
    # Shape alone is not enough: an unknown ses_ id falls through to the legacy
    # readers rather than silently serving an empty transcript.
    assert is_omni_session("ses_deadbeefcafe") is False


def test_known_omni_session_is_detected(mock_db):
    session_id = _seed()
    assert is_omni_session(session_id) is True


# ─── checkpoints ─────────────────────────────────────────────────────────────

def test_checkpoints_from_omni_nodes(mock_db):
    session_id = _seed()
    checkpoints = get_omni_checkpoints(session_id)

    # node_id is the stable rewind handle; the tool-result user node (n4) and
    # the tool_use-only assistant node (n3) are not rewind points.
    assert [(cp.uuid, cp.role) for cp in checkpoints] == [
        ("n1", "user"),
        ("n2", "assistant"),
        ("n5", "assistant"),
        ("n6", "user"),
        ("n7", "assistant"),
    ]
    assert [cp.index for cp in checkpoints] == [0, 1, 2, 3, 4]
    assert checkpoints[0].full_message == "first prompt"
    assert checkpoints[3].message_preview == "second prompt"


def test_entries_stay_aligned_with_the_node_path(mock_db):
    # Load-bearing for rewind: a keep-count over entries maps back positionally
    # to the node that must become the new head.
    session_id = _seed()
    entries = omni_transcript_entries(session_id)
    path = load_omni_path(session_id)
    assert [e["uuid"] for e in entries] == [n["node_id"] for n in path]


def test_entries_empty_for_unknown_session(mock_db):
    assert omni_transcript_entries("ses_nothinghere") == []


# ─── rewind ──────────────────────────────────────────────────────────────────

def test_rewind_keeping_response_moves_head_to_end_of_turn(mock_db):
    session_id = _seed()
    result = rewind_omni_session(session_id, "n1", include_response=True)

    # Keep the first prompt and everything it produced; drop from the next
    # human turn (n6) onward.
    assert result.success is True
    assert _head(session_id) == "n5"
    assert result.messages_removed == 2  # n6, n7
    assert result.remaining_user_turns == 1
    assert [n["node_id"] for n in load_omni_path(session_id)] == [
        "n1", "n2", "n3", "n4", "n5",
    ]


def test_rewind_dropping_target_moves_head_to_its_parent(mock_db):
    session_id = _seed()
    result = rewind_omni_session(session_id, "n6", include_response=False)

    assert result.success is True
    assert _head(session_id) == "n5"
    assert result.messages_removed == 2  # n6 itself, plus n7


def test_rewind_to_first_prompt_can_empty_the_session(mock_db):
    session_id = _seed()
    result = rewind_omni_session(session_id, "n1", include_response=False)

    # A NULL head is a legitimate, resumable omni session — the next turn
    # appends a fresh root node — so the session id is never invalidated.
    assert result.success is True
    assert _head(session_id) is None
    assert load_omni_path(session_id) == []
    assert result.remaining_user_turns == 0


def test_rewind_leaves_dropped_nodes_in_the_tree_off_path(mock_db):
    # omni's rewind semantics: nothing is deleted, so a later turn branches a
    # sibling rather than losing history.
    session_id = _seed()
    rewind_omni_session(session_id, "n6", include_response=False)

    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "SELECT node_id FROM omni_session_nodes WHERE session_id = %s",
            (session_id,),
        )
        stored = {dict(r)["node_id"] for r in cursor.fetchall()}
    assert {"n6", "n7"} <= stored


def test_rewind_unknown_target_fails_without_moving_head(mock_db):
    session_id = _seed()
    result = rewind_omni_session(session_id, "node-that-does-not-exist")

    assert result.success is False
    assert "not found" in (result.error or "")
    assert _head(session_id) == "n7"


def test_rewind_on_missing_session_fails(mock_db):
    result = rewind_omni_session("ses_missing", "n1")
    assert result.success is False


# ─── fork ────────────────────────────────────────────────────────────────────

def test_fork_copies_ancestry_and_heads_at_the_fork_point(mock_db):
    session_id = _seed()
    new_id, node_count = fork_omni_session(session_id, "n5")

    assert new_id.startswith("ses_") and new_id != session_id
    assert node_count == 5
    assert _head(new_id) == "n5"
    # Node ids are preserved verbatim, so the shared prefix stays
    # byte-identical (cache-preserving) and handles stay comparable.
    assert [n["node_id"] for n in load_omni_path(new_id)] == [
        "n1", "n2", "n3", "n4", "n5",
    ]
    # Nothing after the fork point came along...
    assert get_omni_checkpoints(new_id)[-1].uuid == "n5"
    # ...and the source session is untouched.
    assert _head(session_id) == "n7"
    assert len(load_omni_path(session_id)) == 7


def test_fork_at_head_copies_the_whole_session(mock_db):
    session_id = _seed()
    new_id, node_count = fork_omni_session(session_id, "n7")
    assert node_count == 7
    assert omni_transcript_entries(new_id) == [
        {**e, "sessionId": new_id} for e in omni_transcript_entries(session_id)
    ]


def test_fork_rejects_an_off_path_node(mock_db):
    session_id = _seed()
    with pytest.raises(ValueError):
        fork_omni_session(session_id, "nope")


def test_fork_accepts_an_explicit_new_id(mock_db):
    session_id = _seed()
    wanted = new_omni_session_id()
    new_id, _ = fork_omni_session(session_id, "n2", new_sdk_session_id=wanted)
    assert new_id == wanted


# ─── live transcript ─────────────────────────────────────────────────────────

def test_live_messages_render_through_the_shared_transform(mock_db):
    session_id = _seed()
    messages, _usage = omni_live_messages(session_id, "/workspace")

    rendered = [(m["role"], m["type"]) for m in messages]
    # ``type: None`` is the shared transform's own marker for a plain user
    # prompt (same for JSONL and store sources) — proof we render through it
    # rather than through an omni-specific shape.
    assert ("user", None) in rendered
    assert ("assistant", "text") in rendered
    tool_rows = [m for m in messages if m["type"] == "tool_use"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["toolName"] == "Read"
    # The tool result is merged onto its tool card by the shared transform.
    assert tool_rows[0]["toolResult"] == "file body"
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        "first prompt", "second prompt",
    ]


def test_live_messages_empty_for_unknown_session(mock_db):
    assert omni_live_messages("ses_missing") == ([], {})


# ─── rewind content guard (commands.py) ──────────────────────────────────────

def test_content_guard_resolves_an_omni_checkpoint(mock_db):
    from app.api.commands import _resolve_checkpoint_uuid_from_content

    session_id = _seed()
    uuid_, error = _resolve_checkpoint_uuid_from_content(
        session_id, "/workspace", "second prompt", 0
    )
    assert error is None and uuid_ == "n6"


def test_content_guard_still_fails_closed_on_no_match(mock_db):
    from app.api.commands import _resolve_checkpoint_uuid_from_content

    session_id = _seed()
    uuid_, error = _resolve_checkpoint_uuid_from_content(
        session_id, "/workspace", "a prompt that was never sent", 0
    )
    assert uuid_ is None
    assert "0 user prompt(s)" in error

    # Occurrence beyond the matches also refuses.
    uuid_, error = _resolve_checkpoint_uuid_from_content(
        session_id, "/workspace", "second prompt", 3
    )
    assert uuid_ is None and "content guard" in error


# ─── delegated-CLI re-seed (ruling R5) ───────────────────────────────────────
#
# After a rewind the delegated engine's ``claude`` CLI still holds the
# rewound-away turns (it reconnects to its OWN history via ``--resume``). The
# re-seed materializes omni's post-rewind path as a CLI session file under a
# fresh uuid and stores that uuid as the session's resume handle, so the next
# turn reconnects to a conversation that ends at the checkpoint.


def _entry(uuid_, parent, role, content, model=None, timestamp=None):
    """One omni-projected transcript entry (what ``_node_to_entry`` returns)."""
    message = {"role": role, "content": content}
    if model:
        message["model"] = model
    return {
        "uuid": uuid_,
        "parentUuid": parent,
        "type": role,
        "timestamp": timestamp or "2026-08-12T20:00:00+00:00",
        "sessionId": "ses_fixture",
        "message": message,
    }


def _text(text):
    return [{"type": "text", "text": text}]


def _projected_conversation():
    return [
        _entry("nod_1", None, "user", _text("first prompt")),
        _entry("nod_2", "nod_1", "assistant", _text("first reply"), model="claude-x"),
        _entry("nod_3", "nod_2", "assistant", [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}},
        ]),
        _entry("nod_4", "nod_3", "user", [
            {"type": "tool_result", "tool_use_id": "t1", "content": "file body",
             "is_error": False},
        ]),
        _entry("nod_5", "nod_4", "assistant", _text("after tool")),
    ]


def _set_cli(session_id, cli_session_id):
    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        get_cursor(conn).execute(
            "UPDATE omni_sessions SET cli_session_id = %s WHERE session_id = %s",
            (cli_session_id, session_id),
        )


def _read_jsonl(path):
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ─ materialization: entry shape ─

def test_cli_entries_carry_the_fields_the_cli_reads():
    from app.core.provider.omni_transcript import build_cli_transcript_entries

    lines = build_cli_transcript_entries(
        _projected_conversation(), "cli-uuid", "/workspace"
    )

    assert [line["type"] for line in lines] == [
        "user", "assistant", "assistant", "user", "assistant",
    ]
    for line in lines:
        assert set(line) >= {
            "parentUuid", "isSidechain", "userType", "cwd", "sessionId",
            "type", "uuid", "timestamp", "message",
        }
        assert line["isSidechain"] is False
        assert line["userType"] == "external"
        assert line["cwd"] == "/workspace"
        # The CLI's own format ends timestamps in Z, not the +00:00 offset
        # ``datetime.isoformat`` produces.
        assert line["timestamp"].endswith("Z")

    # An assistant entry is a full Anthropic message object (the CLI reads
    # message.role/content and echoes the rest back into its own transcript).
    assistant = lines[1]["message"]
    assert assistant["role"] == "assistant"
    assert assistant["type"] == "message"
    assert assistant["model"] == "claude-x"
    assert assistant["content"] == _text("first reply")
    assert assistant["id"].startswith("msg_")
    assert set(assistant["usage"]) == {
        "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    }
    # A model-less node still produces a valid entry.
    assert lines[2]["message"]["model"] == "<synthetic>"


def test_user_text_turn_becomes_a_plain_string():
    from app.core.provider.omni_transcript import build_cli_transcript_entries

    lines = build_cli_transcript_entries(
        _projected_conversation(), "cli-uuid", "/workspace"
    )
    # How the CLI records a human prompt; multi-block user turns (tool results)
    # keep their block list.
    assert lines[0]["message"] == {"role": "user", "content": "first prompt"}
    assert lines[3]["message"]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "file body",
         "is_error": False},
    ]


def test_uuid_chain_is_fresh_and_unbroken():
    import uuid as _uuidlib

    from app.core.provider.omni_transcript import build_cli_transcript_entries

    entries = _projected_conversation()
    lines = build_cli_transcript_entries(entries, "cli-uuid", "/workspace")

    uuids = [line["uuid"] for line in lines]
    assert len(set(uuids)) == len(uuids)
    # Real uuids, not omni node ids: two conversations must never share
    # message identity, and the CLI expects uuid-shaped ids.
    node_ids = {e["uuid"] for e in entries}
    for value in uuids:
        _uuidlib.UUID(value)
        assert value not in node_ids

    assert lines[0]["parentUuid"] is None
    assert [line["parentUuid"] for line in lines[1:]] == uuids[:-1]


def test_session_id_is_the_new_file_uuid_everywhere():
    from app.core.provider.omni_transcript import build_cli_transcript_entries

    lines = build_cli_transcript_entries(
        _projected_conversation(), "cli-uuid", "/workspace"
    )
    # The CLI keys the resume on the filename and cross-checks this field; the
    # omni session id must not leak into it.
    assert {line["sessionId"] for line in lines} == {"cli-uuid"}


def test_unsigned_thinking_blocks_are_dropped():
    from app.core.provider.omni_transcript import build_cli_transcript_entries

    entries = [
        _entry("nod_1", None, "user", _text("prompt")),
        _entry("nod_2", "nod_1", "assistant", [
            {"type": "thinking", "thinking": "omni carries no signature"},
            {"type": "text", "text": "reply"},
        ]),
        # Thinking-only turn has nothing replayable left → the entry goes too.
        _entry("nod_3", "nod_2", "assistant", [
            {"type": "thinking", "thinking": "more scratch"},
        ]),
    ]
    lines = build_cli_transcript_entries(entries, "cli-uuid", "/workspace")

    assert len(lines) == 2
    assert lines[1]["message"]["content"] == _text("reply")


def test_unpaired_tool_blocks_are_dropped():
    from app.core.provider.omni_transcript import build_cli_transcript_entries

    entries = [
        _entry("nod_1", None, "user", _text("prompt")),
        # A tool_use whose result is not on the path would be rejected on
        # replay; so would an orphan tool_result.
        _entry("nod_2", "nod_1", "assistant", [
            {"type": "text", "text": "calling a tool"},
            {"type": "tool_use", "id": "orphan", "name": "Read", "input": {}},
        ]),
        _entry("nod_3", "nod_2", "user", [
            {"type": "tool_result", "tool_use_id": "never-used", "content": "x"},
        ]),
    ]
    lines = build_cli_transcript_entries(entries, "cli-uuid", "/workspace")

    # The orphan tool_use is stripped from the assistant turn and the orphan
    # tool_result turn drops out entirely.
    assert len(lines) == 2
    assert lines[1]["message"]["content"] == _text("calling a tool")


def test_history_never_opens_on_an_assistant_turn():
    from app.core.provider.omni_transcript import build_cli_transcript_entries

    entries = [
        _entry("nod_1", None, "assistant", _text("stray leading reply")),
        _entry("nod_2", "nod_1", "user", _text("prompt")),
        _entry("nod_3", "nod_2", "assistant", _text("reply")),
    ]
    lines = build_cli_transcript_entries(entries, "cli-uuid", "/workspace")

    assert [line["type"] for line in lines] == ["user", "assistant"]
    assert lines[0]["parentUuid"] is None


def test_empty_transcript_materializes_nothing():
    from app.core.provider.omni_transcript import build_cli_transcript_entries

    assert build_cli_transcript_entries([], "cli-uuid", "/workspace") == []


# ─ full rewind + re-seed flow ─

def test_rewind_then_reseed_writes_the_post_rewind_transcript(mock_db, tmp_path):
    from app.core.jsonl_parser import get_project_dir_name
    from app.core.provider.omni_transcript import (
        get_cli_session_id,
        reseed_delegated_cli_session,
    )

    session_id = _seed()
    _set_cli(session_id, "stale-cli-uuid")

    rewind_omni_session(session_id, "n1", include_response=True)
    result = reseed_delegated_cli_session(session_id, "/workspace", tmp_path)

    assert result.status == "reseeded"
    assert result.context_is_authoritative is True
    new_handle = result.cli_session_id
    assert new_handle and new_handle != "stale-cli-uuid"
    # Stored where the adapter's _resolve_cli_session_id reads it, so the next
    # turn resumes the re-seeded conversation with no extra plumbing.
    assert get_cli_session_id(session_id) == new_handle

    path = tmp_path / get_project_dir_name("/workspace") / f"{new_handle}.jsonl"
    assert path.exists() and str(path) == result.path
    lines = _read_jsonl(path)
    assert len(lines) == result.entries_written
    # Ends at the checkpoint: the second turn (n6/n7) is gone.
    assert [line["type"] for line in lines] == [
        "user", "assistant", "assistant", "user", "assistant",
    ]
    assert lines[0]["message"]["content"] == "first prompt"
    assert "second prompt" not in path.read_text(encoding="utf-8")
    assert {line["sessionId"] for line in lines} == {new_handle}


def test_reseed_is_skipped_without_a_delegated_cli_conversation(mock_db, tmp_path):
    from app.core.provider.omni_transcript import reseed_delegated_cli_session

    session_id = _seed()  # native engine: cli_session_id stays NULL
    result = reseed_delegated_cli_session(session_id, "/workspace", tmp_path)

    # Nothing to diverge from, so nothing is written and no warning is owed.
    assert result.status == "skipped"
    assert result.context_is_authoritative is True
    assert list(tmp_path.iterdir()) == []


def test_reseed_is_skipped_for_a_legacy_session(tmp_path):
    from app.core.provider.omni_transcript import reseed_delegated_cli_session

    # Prefix-gated: a CLI uuid never reaches the database.
    result = reseed_delegated_cli_session(
        "6f1c9e2a-6f0f-4a5e-9a4f-2b6c0d5e7a11", "/workspace", tmp_path
    )
    assert result.status == "skipped"


def test_reseed_drops_the_handle_when_the_rewind_emptied_the_session(
    mock_db, tmp_path
):
    from app.core.provider.omni_transcript import (
        get_cli_session_id,
        reseed_delegated_cli_session,
    )

    session_id = _seed()
    _set_cli(session_id, "stale-cli-uuid")

    # Rewinding past the first prompt leaves a NULL head / empty path.
    rewind_omni_session(session_id, "n1", include_response=False)
    result = reseed_delegated_cli_session(session_id, "/workspace", tmp_path)

    # An empty context IS a fresh CLI conversation — clear the handle rather
    # than write a file the CLI would refuse to resume.
    assert result.status == "cleared"
    assert result.context_is_authoritative is True
    assert get_cli_session_id(session_id) is None
    assert list(tmp_path.iterdir()) == []


def test_reseed_failure_leaves_the_old_handle_in_place(mock_db, tmp_path, monkeypatch):
    from app.core.provider import omni_transcript

    session_id = _seed()
    _set_cli(session_id, "stale-cli-uuid")
    rewind_omni_session(session_id, "n1", include_response=True)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(omni_transcript, "_write_cli_transcript", boom)
    result = omni_transcript.reseed_delegated_cli_session(
        session_id, "/workspace", tmp_path
    )

    # The rewind itself still stands; only the CLI's own history is stale, and
    # the caller keeps the divergence warning (context_is_authoritative False).
    assert result.status == "failed"
    assert result.context_is_authoritative is False
    assert "disk full" in result.error
    assert omni_transcript.get_cli_session_id(session_id) == "stale-cli-uuid"
