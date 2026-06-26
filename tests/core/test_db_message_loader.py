"""
Unit tests for the DB-first message loader's row transform.

Focus: the JSONL entry uuid passthrough that lets the UI rewind by uuid
directly (instead of falling back to fragile content matching). Pure
function — no DB connection required.
"""

from app.core.db_message_loader import _transform_row


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
