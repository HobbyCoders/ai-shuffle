"""Code-mode transcripts: the tool calls a cell made must survive the read.

In code mode the model emits one ``run_python`` tool_use per cell and the tools
the cell calls are dispatched host-side, so they exist only in
``fluid_session_events``. Reading the node tree alone left every non-live
transcript view showing a bare ``run_python`` card. These tests pin the splice
that recovers them — and pin that the default (node-aligned) projection rewind
and fork depend on is untouched.

Rows are seeded with raw SQL in the same shapes ``PgFluidStore`` writes;
tests/core/test_fluid_transcript.py pins the node JSON against fluid's real
serializer, and ``event_to_dict`` (fluid.events.model) is what produces the
event JSON used here.
"""

from __future__ import annotations

import pytest

from app.core.provider import fluid_transcript
from app.core.provider.fluid_transcript import (
    fluid_live_messages,
    fluid_transcript_entries,
    load_fluid_path,
)


def _msg(role, blocks):
    return {"role": role, "content": blocks, "provider_meta": {}}


def _insert_nodes(session_id, nodes):
    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO fluid_sessions (session_id, head_node_id, created_at) "
            "VALUES (%s, %s, %s)",
            (session_id, nodes[-1][0], 1000.0),
        )
        for i, (node_id, parent_id, message) in enumerate(nodes):
            cursor.execute(
                "INSERT INTO fluid_session_nodes "
                "(session_id, node_id, parent_id, ts, message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (session_id, node_id, parent_id, 1000.0 + i, Json(message)),
            )


def _insert_events(session_id, events):
    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cursor = get_cursor(conn)
        for seq, event in enumerate(events, start=1):
            cursor.execute(
                "INSERT INTO fluid_session_events (session_id, seq, event) "
                "VALUES (%s, %s, %s)",
                (session_id, seq, Json(event)),
            )


def _tool_use(tool_use_id, name, input_):
    return {
        "_cls": "ToolUse",
        "_v": 1,
        "session_id": "ses_code",
        "node_id": None,
        "turn_id": "turn_1",
        "seq": -1,  # EventBus assigns the real seq at publish time
        "ts": 0.0,  # like seq, stamped by EventBus.publish; 0.0 pre-publish
        "tool_use_id": tool_use_id,
        "name": name,
        "input": input_,
    }


def _tool_result(tool_use_id, name, output, is_error=False):
    return {
        "_cls": "ToolResult",
        "_v": 1,
        "session_id": "ses_code",
        "node_id": None,
        "turn_id": "turn_1",
        "seq": -1,  # EventBus assigns the real seq at publish time
        "ts": 0.0,  # like seq, stamped by EventBus.publish; 0.0 pre-publish
        "tool_use_id": tool_use_id,
        "name": name,
        "output": output,
        "is_error": is_error,
        "ui": None,
        "images": [],
    }


CELL_CODE = {"code": "from tools.grep import grep\ngrep('needle')"}


def _seed_cell(session_id="ses_code", events=None):
    """One code-mode turn: prompt, run_python cell, its result, final text."""
    _insert_nodes(session_id, [
        ("n1", None, _msg("user", [{"kind": "text", "text": "find the needle"}])),
        ("n2", "n1", _msg("assistant", [{
            "kind": "tooluseblock", "id": "rp1", "name": "run_python",
            "input": CELL_CODE,
        }])),
        ("n3", "n2", _msg("user", [{
            "kind": "toolresultblock", "tool_use_id": "rp1",
            "content": [{"kind": "text", "text": "=> 2 hits"}],
            "is_error": False,
        }])),
        ("n4", "n3", _msg("assistant", [{"kind": "text", "text": "found it"}])),
    ])
    if events is None:
        events = [
            _tool_use("rp1", "run_python", CELL_CODE),
            _tool_use("c1", "Grep", {"pattern": "needle"}),
            _tool_result("c1", "Grep", "app/main.py:12"),
            _tool_use("c2", "Read", {"file_path": "/x"}),
            _tool_result("c2", "Read", "boom", is_error=True),
            _tool_result("rp1", "run_python", "=> 2 hits"),
        ]
    _insert_events(session_id, events)
    return session_id


def _blocks(entry):
    return entry["message"]["content"]


# ─── the default projection is unchanged ─────────────────────────────────────

def test_default_entries_stay_aligned_with_the_node_path(mock_db):
    # Rewind maps a keep-count over entries back to a node positionally, so the
    # default result must never grow a spliced entry.
    session_id = _seed_cell()
    entries = fluid_transcript_entries(session_id)

    assert len(entries) == 4
    assert [e["uuid"] for e in entries] == [
        n["node_id"] for n in load_fluid_path(session_id)
    ]


def test_default_entries_are_byte_identical_to_the_unspliced_projection(mock_db):
    session_id = _seed_cell()
    assert fluid_transcript_entries(session_id) == [
        fluid_transcript._node_to_entry(node, session_id)
        for node in load_fluid_path(session_id)
    ]


# ─── the splice ──────────────────────────────────────────────────────────────

def test_inner_calls_are_spliced_between_the_cell_and_its_result(mock_db):
    session_id = _seed_cell()
    entries = fluid_transcript_entries(session_id, include_inner_calls=True)

    assert [e["uuid"] for e in entries] == [
        "n1", "n2", "evt_c1", "evt_c1_result", "evt_c2", "evt_c2_result",
        "n3", "n4",
    ]
    assert [e["type"] for e in entries] == [
        "user", "assistant", "assistant", "user", "assistant", "user",
        "user", "assistant",
    ]

    assert _blocks(entries[1])[0]["name"] == "run_python"
    assert _blocks(entries[2]) == [{
        "type": "tool_use", "id": "c1", "name": "Grep",
        "input": {"pattern": "needle"},
    }]
    assert _blocks(entries[3]) == [{
        "type": "tool_result", "tool_use_id": "c1",
        "content": "app/main.py:12", "is_error": False,
    }]
    assert _blocks(entries[4]) == [{
        "type": "tool_use", "id": "c2", "name": "Read",
        "input": {"file_path": "/x"},
    }]
    assert _blocks(entries[5]) == [{
        "type": "tool_result", "tool_use_id": "c2",
        "content": "boom", "is_error": True,
    }]
    # The run_python result node still closes the block, after its inner calls.
    assert _blocks(entries[6])[0]["tool_use_id"] == "rp1"


def test_spliced_entries_carry_the_host_entry_context(mock_db):
    session_id = _seed_cell()
    entries = fluid_transcript_entries(session_id, include_inner_calls=True)
    host = entries[1]

    for entry in entries[2:6]:
        assert entry["sessionId"] == session_id
        assert entry["timestamp"] == host["timestamp"]
    # One unbroken parent chain from the run_python entry through the pairs.
    assert [e["parentUuid"] for e in entries[2:6]] == [
        "n2", "evt_c1", "evt_c1_result", "evt_c2",
    ]


def test_inner_tool_use_without_a_result_yields_no_result_entry(mock_db):
    # The cell was interrupted (or the turn died) between dispatch and result.
    session_id = _seed_cell(events=[
        _tool_use("rp1", "run_python", CELL_CODE),
        _tool_use("c1", "Grep", {"pattern": "needle"}),
        _tool_result("rp1", "run_python", "=> 2 hits"),
    ])
    entries = fluid_transcript_entries(session_id, include_inner_calls=True)

    assert [e["uuid"] for e in entries] == ["n1", "n2", "evt_c1", "n3", "n4"]
    assert _blocks(entries[2])[0]["name"] == "Grep"


def test_events_after_the_cell_closes_are_not_attributed_to_it(mock_db):
    # Anything emitted once run_python's own ToolResult landed belongs to the
    # engine, not to the cell.
    session_id = _seed_cell(events=[
        _tool_use("rp1", "run_python", CELL_CODE),
        _tool_result("rp1", "run_python", "=> 2 hits"),
        _tool_use("stray", "Bash", {"command": "ls"}),
        _tool_result("stray", "Bash", "a\nb"),
    ])
    entries = fluid_transcript_entries(session_id, include_inner_calls=True)

    assert [e["uuid"] for e in entries] == ["n1", "n2", "n3", "n4"]


def test_two_cells_in_one_message_keep_their_own_inner_calls(mock_db):
    # The engine emits BOTH outer ToolUses while the message is still streaming
    # and only then dispatches them one at a time, so the log interleaves as
    # use(rp1), use(rp2), <rp1's calls>, result(rp1), <rp2's calls>, result(rp2).
    # Attribution follows the oldest OPEN block, not the last one seen.
    session_id = "ses_two_cells"
    _insert_nodes(session_id, [
        ("n1", None, _msg("user", [{"kind": "text", "text": "do both"}])),
        ("n2", "n1", _msg("assistant", [
            {"kind": "tooluseblock", "id": "rp1", "name": "run_python",
             "input": CELL_CODE},
            {"kind": "tooluseblock", "id": "rp2", "name": "run_python",
             "input": {"code": "read()"}},
        ])),
        ("n3", "n2", _msg("user", [
            {"kind": "toolresultblock", "tool_use_id": "rp1",
             "content": [{"kind": "text", "text": "=> 2 hits"}], "is_error": False},
            {"kind": "toolresultblock", "tool_use_id": "rp2",
             "content": [{"kind": "text", "text": "=> ok"}], "is_error": False},
        ])),
        ("n4", "n3", _msg("assistant", [{"kind": "text", "text": "done"}])),
    ])
    _insert_events(session_id, [
        _tool_use("rp1", "run_python", CELL_CODE),
        _tool_use("rp2", "run_python", {"code": "read()"}),
        _tool_use("c1", "Grep", {"pattern": "needle"}),
        _tool_result("c1", "Grep", "app/main.py:12"),
        _tool_result("rp1", "run_python", "=> 2 hits"),
        _tool_use("c2", "Read", {"file_path": "/x"}),
        _tool_result("c2", "Read", "file body"),
        _tool_result("rp2", "run_python", "=> ok"),
    ])

    calls = fluid_transcript._inner_calls_by_block(
        fluid_transcript._event_rows(session_id), {"rp1", "rp2"}
    )
    assert [r["id"] for r in calls["rp1"]] == ["c1"]
    assert [r["id"] for r in calls["rp2"]] == ["c2"]

    entries = fluid_transcript_entries(session_id, include_inner_calls=True)
    # One assistant node holds both blocks, so both blocks' inner records land
    # right after that single entry — rp1's first, in block order.
    assert [e["uuid"] for e in entries] == [
        "n1", "n2",
        "evt_c1", "evt_c1_result", "evt_c2", "evt_c2_result",
        "n3", "n4",
    ]
    assert _blocks(entries[2])[0]["name"] == "Grep"
    assert _blocks(entries[4])[0]["name"] == "Read"
    assert _blocks(entries[3])[0]["content"] == "app/main.py:12"
    assert _blocks(entries[5])[0]["content"] == "file body"
    # The chain stays unbroken across the two blocks' pairs.
    assert [e["parentUuid"] for e in entries[2:6]] == [
        "n2", "evt_c1", "evt_c1_result", "evt_c2",
    ]


def test_a_sibling_non_cell_tool_gets_no_inner_calls(mock_db):
    # run_python and a plain Read in the same message: the Read's ToolUse is
    # streamed before run_python is dispatched, so the cell's inner calls must
    # still be attributed to the cell, and the Read must get no splice at all.
    session_id = "ses_cell_and_read"
    _insert_nodes(session_id, [
        ("n1", None, _msg("user", [{"kind": "text", "text": "grep and read"}])),
        ("n2", "n1", _msg("assistant", [
            {"kind": "tooluseblock", "id": "rp1", "name": "run_python",
             "input": CELL_CODE},
            {"kind": "tooluseblock", "id": "r1", "name": "Read",
             "input": {"file_path": "/y"}},
        ])),
        ("n3", "n2", _msg("user", [
            {"kind": "toolresultblock", "tool_use_id": "rp1",
             "content": [{"kind": "text", "text": "=> 2 hits"}], "is_error": False},
            {"kind": "toolresultblock", "tool_use_id": "r1",
             "content": [{"kind": "text", "text": "y body"}], "is_error": False},
        ])),
        ("n4", "n3", _msg("assistant", [{"kind": "text", "text": "done"}])),
    ])
    _insert_events(session_id, [
        _tool_use("rp1", "run_python", CELL_CODE),
        _tool_use("r1", "Read", {"file_path": "/y"}),
        _tool_use("c1", "Grep", {"pattern": "needle"}),
        _tool_result("c1", "Grep", "app/main.py:12"),
        _tool_result("rp1", "run_python", "=> 2 hits"),
        _tool_result("r1", "Read", "y body"),
    ])

    calls = fluid_transcript._inner_calls_by_block(
        fluid_transcript._event_rows(session_id), {"rp1", "r1"}
    )
    assert [r["id"] for r in calls["rp1"]] == ["c1"]
    assert "r1" not in calls

    entries = fluid_transcript_entries(session_id, include_inner_calls=True)
    assert [e["uuid"] for e in entries] == [
        "n1", "n2", "evt_c1", "evt_c1_result", "n3", "n4",
    ]
    assert _blocks(entries[2])[0]["name"] == "Grep"


def test_a_session_without_cells_never_reads_the_event_log(mock_db, monkeypatch):
    session_id = "ses_plain"
    _insert_nodes(session_id, [
        ("n1", None, _msg("user", [{"kind": "text", "text": "read it"}])),
        ("n2", "n1", _msg("assistant", [{
            "kind": "tooluseblock", "id": "t1", "name": "Read",
            "input": {"file_path": "/x"},
        }])),
        ("n3", "n2", _msg("user", [{
            "kind": "toolresultblock", "tool_use_id": "t1",
            "content": [{"kind": "text", "text": "file body"}],
            "is_error": False,
        }])),
    ])

    def boom(*args, **kwargs):
        raise AssertionError("the event log must not be queried without a cell")

    monkeypatch.setattr(fluid_transcript, "_event_rows", boom)
    entries = fluid_transcript_entries(session_id, include_inner_calls=True)
    assert [e["uuid"] for e in entries] == ["n1", "n2", "n3"]


def test_splice_is_empty_for_an_unknown_session(mock_db):
    assert fluid_transcript_entries("ses_nothinghere", include_inner_calls=True) == []


# ─── rendering ───────────────────────────────────────────────────────────────

def test_live_messages_show_a_card_per_inner_call(mock_db):
    session_id = _seed_cell()
    messages, _usage = fluid_live_messages(session_id, "/workspace")

    tool_rows = [m for m in messages if m["type"] == "tool_use"]
    assert [m["toolName"] for m in tool_rows] == ["run_python", "Grep", "Read"]
    # Results merge onto their own card, exactly as a JSON-surface call does.
    assert tool_rows[1]["toolResult"] == "app/main.py:12"
    assert tool_rows[2]["toolResult"] == "boom"
    assert [m["content"] for m in messages if m["role"] == "user"] == [
        "find the needle",
    ]


def test_inner_calls_by_block_ignores_unrelated_events(mock_db):
    # Non-tool events (the bulk of the log) must not disturb the walk.
    calls = fluid_transcript._inner_calls_by_block(
        [
            {"seq": 1, "event": _tool_use("rp1", "run_python", CELL_CODE)},
            {"seq": 2, "event": {"_cls": "TextDelta", "text": "hi"}},
            {"seq": 3, "event": _tool_use("c1", "Grep", {"pattern": "x"})},
            {"seq": 4, "event": _tool_result("c1", "Grep", "hit")},
            {"seq": 5, "event": _tool_result("rp1", "run_python", "done")},
        ],
        {"rp1"},
    )
    assert calls == {"rp1": [{
        "id": "c1", "name": "Grep", "input": {"pattern": "x"},
        "output": "hit", "is_error": False,
    }]}


def test_event_fixture_matches_real_fluid_serialization():
    """The hand-written event JSON must equal what fluid actually persists."""
    pytest.importorskip("fluid")
    from fluid.events.model import ToolResult, ToolUse, event_to_dict

    assert event_to_dict(ToolUse(
        session_id="ses_code", turn_id="turn_1", tool_use_id="c1",
        name="Grep", input={"pattern": "needle"},
    )) == _tool_use("c1", "Grep", {"pattern": "needle"})
    assert event_to_dict(ToolResult(
        session_id="ses_code", turn_id="turn_1", tool_use_id="c1",
        name="Grep", output="app/main.py:12", is_error=False,
    )) == _tool_result("c1", "Grep", "app/main.py:12")


def test_legacy_tool_result_without_images_still_replays():
    pytest.importorskip("fluid")
    from fluid.events.model import ToolResult, event_from_dict

    legacy_result = _tool_result("c1", "Grep", "app/main.py:12")
    legacy_result.pop("images")
    restored = event_from_dict(legacy_result)
    assert isinstance(restored, ToolResult)
    assert restored.images == []

    calls = fluid_transcript._inner_calls_by_block([
        {"seq": 1, "event": _tool_use("rp1", "run_python", CELL_CODE)},
        {"seq": 2, "event": _tool_use("c1", "Grep", {"pattern": "needle"})},
        {"seq": 3, "event": legacy_result},
        {"seq": 4, "event": _tool_result("rp1", "run_python", "done")},
    ], {"rp1"})
    assert calls == {"rp1": [{
        "id": "c1", "name": "Grep", "input": {"pattern": "needle"},
        "output": "app/main.py:12", "is_error": False,
    }]}


def test_fork_at_head_preserves_inner_calls(mock_db):
    # Inner Grep/Read cards live only in the event log. A fork that copied
    # nodes and dropped events would render a bare run_python card.
    from app.core.provider.fluid_transcript import fork_fluid_session

    session_id = _seed_cell()
    new_id, _ = fork_fluid_session(session_id, "n4")

    parent = fluid_transcript_entries(session_id, include_inner_calls=True)
    forked = fluid_transcript_entries(new_id, include_inner_calls=True)
    assert [e["uuid"] for e in forked] == [e["uuid"] for e in parent]
    assert [e["type"] for e in forked] == [e["type"] for e in parent]
    assert _blocks(forked[2])[0]["name"] == "Grep"
    assert _blocks(forked[4])[0]["name"] == "Read"
