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


def test_subagent_completed_report_fields_and_legacy_defaults():
    adapter = TypeAdapter(SessionEvent)
    legacy = dict(
        type="subagent.completed", id="e1", session_id="s1", seq=1,
        created_at="2026-07-11T00:00:00Z", task_id="t1", status="completed",
        result_preview="preview",
    )
    event = adapter.validate_python(legacy)
    assert event.result is None
    assert event.result_truncated is False
    full = {**legacy, "result": "full report", "result_truncated": True}
    assert adapter.validate_json(adapter.dump_json(adapter.validate_python(full))).result == "full report"
    assert adapter.validate_python(full).result_truncated is True


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
            "type": "system.compaction_status",
            "id": "e7",
            "session_id": "s1",
            "seq": 7,
            "created_at": "2026-07-11T00:00:06Z",
            "state": "started",
            "trigger": "auto",
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


@pytest.mark.asyncio
async def test_swarm_coordinator_snapshot_round_trips_in_event_contract():
    from fluid.swarm import SwarmCoordinator
    from fluid.tools.registry import ToolCtx

    emitted = []

    async def publish(snapshot):
        emitted.append(snapshot)

    async def dispatch(data, ctx):
        return "Verified task output"

    async def cancel_worker(task_id):
        pass

    async def finished(*args):
        pass

    coordinator = SwarmCoordinator("s1", dispatch, cancel_worker, publish, finished)
    snapshot, _ = await coordinator.start({"title": "Contract workflow", "tasks": [
        {"id": "check", "agent": "reviewer", "prompt": "Check the output"},
    ]}, ToolCtx(cwd=".", session_id="s1"))
    await coordinator.jobs[snapshot["id"]]
    adapter = TypeAdapter(SessionEvent)
    for seq, swarm in enumerate(emitted, 1):
        event = adapter.validate_python({
            "type": "swarm.updated", "id": f"e{seq}", "session_id": "s1",
            "seq": seq, "created_at": swarm["updated_at"], "swarm": swarm,
        })
        assert adapter.validate_json(event.model_dump_json()) == event
    assert emitted[-1]["tasks"][0]["result"] == "Verified task output"
    assert emitted[-1]["status"] == "completed"


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


def test_turn_started_carries_the_turn_source():
    """#6: the app's turn.started carries what started the turn, so wake
    metrics can be read from the app event log as well as fluid's."""
    event = TypeAdapter(SessionEvent).validate_python({
        "type": "turn.started", "id": "e1", "session_id": "s1", "seq": 1,
        "created_at": "2026-09-23T00:00:00Z", "source": "unpark",
    })
    assert event.source == "unpark"
