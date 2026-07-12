"""Tests for the V2 event/command contract and its schema export."""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.contract import Command, SessionEvent
from app.contract.export import (
    SCHEMA_DIR,
    _DUMP_KWARGS,
    build_commands_schema,
    build_events_schema,
)

EVENTS_SCHEMA_PATH = SCHEMA_DIR / "events.schema.json"
COMMANDS_SCHEMA_PATH = SCHEMA_DIR / "commands.schema.json"


def _dump(schema: dict) -> str:
    return json.dumps(schema, **_DUMP_KWARGS) + "\n"


def test_committed_schemas_match_models():
    """Editing models without regenerating the committed schemas must fail CI."""
    assert _dump(build_events_schema()) == Path(EVENTS_SCHEMA_PATH).read_text(
        encoding="utf-8"
    )
    assert _dump(build_commands_schema()) == Path(COMMANDS_SCHEMA_PATH).read_text(
        encoding="utf-8"
    )


def test_round_trip_discriminated_dispatch():
    event_adapter = TypeAdapter(SessionEvent)
    command_adapter = TypeAdapter(Command)

    event_payloads = [
        {
            "type": "turn.started",
            "id": "e1",
            "session_id": "s1",
            "seq": 1,
            "created_at": "2026-07-11T00:00:00Z",
        },
        {
            "type": "message.user",
            "id": "e2",
            "session_id": "s1",
            "seq": 2,
            "created_at": "2026-07-11T00:00:01Z",
            "content": "hello",
            "attachments": ["abc123"],
        },
        {
            "type": "assistant.text_delta",
            "id": "e3",
            "session_id": "s1",
            "seq": 3,
            "created_at": "2026-07-11T00:00:02Z",
            "text": "hi there",
        },
        {
            "type": "tool.use",
            "id": "e4",
            "session_id": "s1",
            "seq": 4,
            "created_at": "2026-07-11T00:00:03Z",
            "tool_use_id": "tu1",
            "name": "Bash",
            "input": {"command": "ls"},
        },
        {
            "type": "subagent.completed",
            "id": "e5",
            "session_id": "s1",
            "seq": 5,
            "created_at": "2026-07-11T00:00:04Z",
            "task_id": "t1",
            "status": "completed",
        },
        {
            "type": "artifact.created",
            "id": "e6",
            "session_id": "s1",
            "seq": 6,
            "created_at": "2026-07-11T00:00:05Z",
            "hash": "deadbeef",
            "size": 42,
        },
    ]

    for payload in event_payloads:
        model = event_adapter.validate_python(payload)
        assert model.type == payload["type"]
        round_tripped = event_adapter.validate_python(model.model_dump())
        assert round_tripped.type == payload["type"]
        assert round_tripped.model_dump() == model.model_dump()

    command_payloads = [
        {
            "type": "send_message",
            "command_id": "c1",
            "session_id": "s1",
            "content": "do the thing",
            "attachments": ["h1"],
        },
        {
            "type": "interrupt",
            "command_id": "c2",
            "session_id": "s1",
            "mode": "stop",
        },
    ]

    for payload in command_payloads:
        model = command_adapter.validate_python(payload)
        assert model.type == payload["type"]
        round_tripped = command_adapter.validate_python(model.model_dump())
        assert round_tripped.type == payload["type"]
        assert round_tripped.model_dump() == model.model_dump()


def test_unknown_type_raises():
    with pytest.raises(ValidationError):
        TypeAdapter(SessionEvent).validate_python(
            {
                "type": "does.not.exist",
                "id": "e1",
                "session_id": "s1",
                "seq": 1,
                "created_at": "2026-07-11T00:00:00Z",
            }
        )
    with pytest.raises(ValidationError):
        TypeAdapter(Command).validate_python(
            {
                "type": "does_not_exist",
                "command_id": "c1",
                "session_id": "s1",
            }
        )
