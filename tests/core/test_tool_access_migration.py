"""v82: stamp ``tool_selection_mode`` and fold the retired switches (T2/T5/T8).

The migration runs in the always-run column-migration pass, so it must be
idempotent and must never change an agent whose intent is already explicit.
"""

import json

from app.db import database
from app.db.database import _migrate_agent_tool_access


def _agent(mock_db, agent_id, config):
    database.create_profile(
        profile_id=agent_id, name=agent_id, description=None, config=config
    )


def _config(mock_db, agent_id):
    row = mock_db.execute(
        "SELECT config FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    raw = row["config"]
    return json.loads(raw) if isinstance(raw, str) else raw


def _run(mock_db):
    from tests.pg_shim import ShimConnection

    conn = ShimConnection(mock_db)
    cursor = conn.cursor()
    _migrate_agent_tool_access(cursor)
    conn.commit()


def test_empty_allow_list_becomes_mode_none(mock_db):
    """The headline bug: "Disable all tools" saved ``allowed_tools: []``, which
    every runtime path read as unrestricted. It is stamped as ``none``."""
    _agent(mock_db, "disabled", {"allowed_tools": []})
    _run(mock_db)
    assert _config(mock_db, "disabled")["tool_selection_mode"] == "none"


def test_non_empty_lists_keep_their_inferred_mode(mock_db):
    _agent(mock_db, "allower", {"allowed_tools": ["Read"], "app_config_tools": True})
    _agent(mock_db, "denier", {"disallowed_tools": ["Bash"], "app_config_tools": True})
    _run(mock_db)
    assert _config(mock_db, "allower")["tool_selection_mode"] == "allow"
    assert _config(mock_db, "allower")["allowed_tools"] == ["Read"]
    assert _config(mock_db, "denier")["tool_selection_mode"] == "disallow"
    assert _config(mock_db, "denier")["disallowed_tools"] == ["Bash"]


def test_app_config_switch_off_folds_into_a_deny(mock_db):
    _agent(mock_db, "noconfig", {"app_config_tools": False})
    _run(mock_db)
    config = _config(mock_db, "noconfig")
    assert config["tool_selection_mode"] == "disallow"
    assert "mcp__app-config__*" in config["disallowed_tools"]


def test_disabled_ai_categories_fold_into_denies(mock_db):
    _agent(
        mock_db,
        "partialai",
        {"app_config_tools": True, "ai_tools": {"image_generation": True}},
    )
    _run(mock_db)
    config = _config(mock_db, "partialai")
    assert config["tool_selection_mode"] == "disallow"
    denied = config["disallowed_tools"]
    assert "mcp__ai-tools__generateVideo" in denied
    assert "mcp__ai-tools__generateImage" not in denied
    # The source field survives for import compatibility.
    assert config["ai_tools"] == {"image_generation": True}


def test_allow_mode_drops_disabled_ai_tools_from_the_list(mock_db):
    _agent(
        mock_db,
        "allowai",
        {
            "allowed_tools": [
                "Read",
                "mcp__ai-tools__generateImage",
                "mcp__ai-tools__generateVideo",
            ],
            "ai_tools": {"image_generation": True},
        },
    )
    _run(mock_db)
    config = _config(mock_db, "allowai")
    assert config["tool_selection_mode"] == "allow"
    assert config["allowed_tools"] == ["Read", "mcp__ai-tools__generateImage"]


def test_an_already_stamped_agent_is_left_alone(mock_db):
    _agent(
        mock_db,
        "explicit",
        {"tool_selection_mode": "all", "app_config_tools": False},
    )
    _run(mock_db)
    config = _config(mock_db, "explicit")
    assert config["tool_selection_mode"] == "all"
    assert not config.get("disallowed_tools")


def test_migration_is_idempotent(mock_db):
    _agent(mock_db, "twice", {"app_config_tools": False})
    _run(mock_db)
    first = _config(mock_db, "twice")
    _run(mock_db)
    assert _config(mock_db, "twice") == first
