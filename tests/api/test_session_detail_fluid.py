"""GET /api/v1/sessions/{id} must serve a fluid (flag-on) session's transcript.

Same omission as the event-feed backfill: the detail read walks the store /
JSONL parsers, both blind to fluid's parent-linked node DAG, so a flag-on
session's non-live detail read returned an empty (or legacy user-rows-only)
transcript. The /live path grew this branch in PR #815; this pins it here too.

Rows are seeded exactly as ``PgFluidStore`` writes them — the fixture shape is
pinned against fluid's real serializer in tests/core/test_fluid_transcript.py.
"""

from types import SimpleNamespace

from app.api.sessions import get_session
from app.db import sessions as sessions_db
from tests.helpers import fluid_message

SID = "sess-fluid-detail"
SDK_ID = "ses_detail0001"


def _request():
    """Admin request (no api_user) — check_session_access is then a no-op."""
    return SimpleNamespace(state=SimpleNamespace(api_user=None))


def _text(role, text):
    return fluid_message(role, [{"kind": "text", "text": text}])


def _seed_fluid(nodes):
    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db

    sessions_db.create_session(SID, "default")
    sessions_db.update_session(SID, sdk_session_id=SDK_ID)

    with get_db() as conn:
        cursor = get_cursor(conn)
        cursor.execute(
            "INSERT INTO fluid_sessions (session_id, head_node_id, created_at) "
            "VALUES (%s, %s, %s)",
            (SDK_ID, nodes[-1][0], 1000.0),
        )
        for i, (node_id, parent_id, message) in enumerate(nodes):
            cursor.execute(
                "INSERT INTO fluid_session_nodes "
                "(session_id, node_id, parent_id, ts, message) "
                "VALUES (%s, %s, %s, %s, %s)",
                (SDK_ID, node_id, parent_id, 1000.0 + i, Json(message)),
            )


async def test_detail_read_serves_the_fluid_transcript(mock_db):
    _seed_fluid([
        ("n1", None, _text("user", "fluid prompt")),
        ("n2", "n1", _text("assistant", "fluid reply")),
    ])

    result = await get_session(request=_request(), session_id=SID, token="tok")

    assert [(m["role"], m["content"]) for m in result["messages"]] == [
        ("user", "fluid prompt"),
        ("assistant", "fluid reply"),
    ]


async def test_detail_read_renders_fluid_tool_cards_through_the_shared_transform(mock_db):
    """The fluid branch runs through the same ``_transform_entries`` core as
    every other source, so a tool call renders as a merged tool card."""
    _seed_fluid([
        ("n1", None, _text("user", "read it")),
        ("n2", "n1", fluid_message("assistant", [{
            "kind": "tooluseblock", "id": "t1", "name": "Read",
            "input": {"file_path": "/x"},
        }])),
        ("n3", "n2", fluid_message("user", [{
            "kind": "toolresultblock", "tool_use_id": "t1",
            "content": [{"kind": "text", "text": "file body"}],
            "is_error": False,
        }])),
    ])

    result = await get_session(request=_request(), session_id=SID, token="tok")

    tool_rows = [m for m in result["messages"] if m.get("type") == "tool_use"]
    assert len(tool_rows) == 1
    assert tool_rows[0]["toolName"] == "Read"
    assert tool_rows[0]["toolResult"] == "file body"


def _seed_events(events):
    from psycopg2.extras import Json

    from app.db.base import get_cursor, get_db

    with get_db() as conn:
        cursor = get_cursor(conn)
        for seq, event in enumerate(events, start=1):
            cursor.execute(
                "INSERT INTO fluid_session_events (session_id, seq, event) "
                "VALUES (%s, %s, %s)",
                (SDK_ID, seq, Json(event)),
            )


def _event(cls, tool_use_id, name, **extra):
    """One persisted fluid event (``fluid.events.model.event_to_dict``)."""
    return {
        "_cls": cls,
        "session_id": SDK_ID,
        "node_id": None,
        "turn_id": "turn_1",
        "seq": -1,
        "tool_use_id": tool_use_id,
        "name": name,
        **extra,
    }


async def test_detail_read_lists_the_tools_a_code_mode_cell_ran(mock_db):
    """In code mode the model emits one ``run_python`` block per cell and the
    tools it calls live only in the event log — the detail read must show them
    as their own cards, not as a single opaque cell."""
    cell = {"code": "grep('needle')"}
    _seed_fluid([
        ("n1", None, _text("user", "find the needle")),
        ("n2", "n1", fluid_message("assistant", [{
            "kind": "tooluseblock", "id": "rp1", "name": "run_python",
            "input": cell,
        }])),
        ("n3", "n2", fluid_message("user", [{
            "kind": "toolresultblock", "tool_use_id": "rp1",
            "content": [{"kind": "text", "text": "=> 2 hits"}],
            "is_error": False,
        }])),
    ])
    _seed_events([
        _event("ToolUse", "rp1", "run_python", input=cell),
        _event("ToolUse", "c1", "Grep", input={"pattern": "needle"}),
        _event("ToolResult", "c1", "Grep", output="app/main.py:12",
               is_error=False, ui=None),
        _event("ToolUse", "c2", "Read", input={"file_path": "/x"}),
        _event("ToolResult", "c2", "Read", output="file body",
               is_error=False, ui=None),
        _event("ToolResult", "rp1", "run_python", output="=> 2 hits",
               is_error=False, ui=None),
    ])

    result = await get_session(request=_request(), session_id=SID, token="tok")

    tool_rows = [m for m in result["messages"] if m.get("type") == "tool_use"]
    assert [m["toolName"] for m in tool_rows] == ["run_python", "Grep", "Read"]
    assert tool_rows[1]["toolResult"] == "app/main.py:12"
    assert tool_rows[2]["toolResult"] == "file body"
