"""The ``enable_file_checkpointing`` agent flag is retired.

It once asked the Agent SDK to keep file checkpoints so rewind could restore
files. Nothing drives the SDK any more and the rewind endpoint refuses file
restore for every session, yet the flag still made the rewind dialog offer a
"Chat & Code" option that always failed. Stored rows that carry it must still
load, the key must not be read back out of the agent store, and profile
writes (including an older client that still sends it) must not store it.
"""

from __future__ import annotations

import json

import pytest

from app.db import database
from app.db.base import get_db

ABSENT = object()


def _config(flag):
    config = {
        "system_prompt": {"type": "custom", "content": "Custom agent prompt"},
        "allowed_tools": ["Read"],
        "tool_selection_mode": "allow",
        "max_turns": 7,
    }
    if flag is not ABSENT:
        config["enable_file_checkpointing"] = flag
    return config


def _insert_raw(agent_id, config):
    """Store a row the way an older build (or an older peer) left it."""
    with get_db() as conn:
        conn.cursor().execute(
            "INSERT INTO agents (id, name, description, config, is_builtin, mcp_tools, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (agent_id, agent_id, None, json.dumps(config), False, "[]",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )


def _raw_config(agent_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT config FROM agents WHERE id = %s", (agent_id,))
        raw = cursor.fetchone()["config"]
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.mark.parametrize("flag", [True, False])
def test_old_rows_load_without_the_flag(flag):
    agent_id = f"fc-read-{str(flag).lower()}"
    _insert_raw(agent_id, _config(flag))

    assert database.get_profile(agent_id)["config"] == _config(ABSENT)
    listed = {p["id"]: p for p in database.get_all_profiles()}
    assert listed[agent_id]["config"] == _config(ABSENT)


def test_writes_never_store_the_flag():
    database.create_profile("fc-write", "W", None, _config(True))
    assert _raw_config("fc-write") == _config(ABSENT)

    database.update_profile("fc-write", config={**_config(True), "max_turns": 9})
    assert _raw_config("fc-write") == {**_config(ABSENT), "max_turns": 9}


def test_update_of_an_old_row_drops_the_flag():
    """The editor merge keeps keys its schema doesn't know, the retired flag
    included when it starts from the raw row; the store still drops it."""
    from app.core.profiles import preserve_profile_presentation

    _insert_raw("fc-old", _config(True))
    existing = _raw_config("fc-old")
    merged = preserve_profile_presentation(existing, {**_config(ABSENT), "max_turns": 3})
    database.update_profile("fc-old", config=merged)
    stored = _raw_config("fc-old")
    assert "enable_file_checkpointing" not in stored
    assert stored["max_turns"] == 3


def test_api_schema_accepts_and_drops_the_flag():
    """An older client still sending the flag is accepted; the key never comes back."""
    from app.core.models import ProfileConfig, ProfileCreate, ProfileUpdate

    assert "enable_file_checkpointing" not in ProfileConfig.model_fields
    created = ProfileCreate.model_validate({"name": "A", "config": _config(True)})
    assert "enable_file_checkpointing" not in created.config.model_dump()
    updated = ProfileUpdate.model_validate({"config": _config(True)})
    assert updated.config is not None
    assert "enable_file_checkpointing" not in updated.config.model_dump()
