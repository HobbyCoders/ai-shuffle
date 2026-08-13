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
