"""
Unit tests for the DB-first message loader's row transform.

Focus: the JSONL entry uuid passthrough that lets the UI rewind by uuid
directly (instead of falling back to fragile content matching). Pure
function — no DB connection required.
"""

from app.core import db_message_loader
from app.core.db_message_loader import _transform_row, load_messages_from_db


def _row(**overrides):
    base = {
        "id": 7,
        "role": "user",
        "content": "hello world",
        "metadata": {},
        "created_at": "2026-06-25T00:00:00",
    }
    base.update(overrides)
    return base


def test_user_row_surfaces_jsonl_uuid_as_uuid():
    """A backfilled user row exposes its JSONL uuid so rewind can target it."""
    msg = _transform_row(_row(jsonl_uuid="uuid-abc"))
    assert msg["uuid"] == "uuid-abc"
    # id stays the DB-derived key (not a valid rewind target).
    assert msg["id"] == "msg-7"


def test_user_row_without_jsonl_uuid_has_no_uuid_key():
    """Rows persisted before backfill must not carry a bogus uuid."""
    msg = _transform_row(_row())
    assert "uuid" not in msg

    msg_null = _transform_row(_row(jsonl_uuid=None))
    assert "uuid" not in msg_null


def test_assistant_row_surfaces_jsonl_uuid():
    msg = _transform_row(
        _row(role="assistant", content="hi there", jsonl_uuid="uuid-xyz")
    )
    assert msg["uuid"] == "uuid-xyz"
    assert msg["role"] == "assistant"


def test_subagent_row_surfaces_agent_usage_from_meta():
    """A persisted subagent card exposes its stored agent_usage as agentUsage."""
    usage = {"total_tokens": 42, "tool_uses": 3, "duration_ms": 100}
    msg = _transform_row(_row(
        role="subagent",
        content="done",
        tool_name="Task",
        metadata={"tool_id": "task-9", "agent_status": "completed", "agent_usage": usage},
    ))
    assert msg["type"] == "subagent"
    assert msg["agentUsage"] == usage


def test_subagent_row_without_usage_has_none_agent_usage():
    msg = _transform_row(_row(
        role="subagent", content="done", tool_name="Task",
        metadata={"tool_id": "task-9", "agent_status": "completed"},
    ))
    assert msg["agentUsage"] is None


def _notification_content(tool_id="task-bg", status="completed",
                          summary="short", result="FULL RESULT"):
    return (
        f"<task-notification>\n"
        f"  <tool-use-id>{tool_id}</tool-use-id>\n"
        f"  <status>{status}</status>\n"
        f"  <summary>{summary}</summary>\n"
        f"  <result>{result}</result>\n"
        f"  <usage>\n"
        f"    <subagent_tokens>777</subagent_tokens>\n"
        f"    <tool_uses>2</tool_uses>\n"
        f"    <duration_ms>500</duration_ms>\n"
        f"  </usage>\n"
        f"</task-notification>"
    )


def test_persisted_task_notification_repairs_subagent_card(monkeypatch):
    """A <task-notification> user row flips the matching card's status and
    applies usage + result on DB reload (parked-completion recovery)."""
    rows = [
        {
            "id": 1, "role": "subagent", "content": "", "tool_name": "Task",
            "metadata": {"tool_id": "task-bg", "agent_status": "running"},
            "created_at": "2026-06-25T00:00:00",
        },
        {
            "id": 2, "role": "user", "content": _notification_content(),
            "metadata": {}, "created_at": "2026-06-25T00:00:01",
        },
    ]
    monkeypatch.setattr(
        db_message_loader.database, "get_session_messages", lambda _sid: rows)

    messages, has_db = load_messages_from_db("sess-1")
    assert has_db is True
    subs = [m for m in messages if m.get("type") == "subagent"]
    assert len(subs) == 1
    assert subs[0]["agentStatus"] == "completed"
    assert subs[0]["content"] == "FULL RESULT"
    assert subs[0]["agentUsage"] == {
        "total_tokens": 777, "tool_uses": 2, "duration_ms": 500,
    }


def test_persisted_failed_notification_marks_card_error(monkeypatch):
    rows = [
        {
            "id": 1, "role": "subagent", "content": "", "tool_name": "Task",
            "metadata": {"tool_id": "task-bg", "agent_status": "running"},
            "created_at": "2026-06-25T00:00:00",
        },
        {
            "id": 2, "role": "user",
            "content": _notification_content(status="failed"),
            "metadata": {}, "created_at": "2026-06-25T00:00:01",
        },
    ]
    monkeypatch.setattr(
        db_message_loader.database, "get_session_messages", lambda _sid: rows)
    messages, _ = load_messages_from_db("sess-1")
    subs = [m for m in messages if m.get("type") == "subagent"]
    assert subs[0]["agentStatus"] == "error"
