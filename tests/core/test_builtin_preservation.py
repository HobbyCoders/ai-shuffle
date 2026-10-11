"""Conservative upgrade preservation for the two retired starter roles."""
import copy
import hashlib

import pytest

from app.core.builtin_subagents import seed_builtin_subagents
from app.core.builtin_retirement import is_untouched_builtin
from app.db import database
from tests.builtin_fixtures import create_unchanged_builtin

ROLES = ("general-purpose", "frontend-designer")
EDITS = ("name", "description", "prompt", "model", "tools", "provider_models",
         "memory", "skills", "mcp_servers", "enabled_agents", "mcp_tools",
         "avatar_color", "permissions", "unknown", "unmatchable", "future_record")


def _record(agent, revision=2):
    values = {"name": agent["name"], "description": agent["description"],
              "prompt": agent["config"]["system_prompt"]["content"]}
    return {"revision": revision, "sha256": {
        key: hashlib.sha256(value.encode()).hexdigest() for key, value in values.items()
    }}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("revision", [None, 1, 2])
@pytest.mark.parametrize("migrated", [False, True])
def test_known_defaults_retire(mock_db, role, revision, migrated):
    agent = create_unchanged_builtin(role)
    # Revision 1 still suggested Plan. Revision 2 changed this one field.
    if revision == 1 and role == "general-purpose":
        agent["description"] = agent["description"].replace(
            "rendered UI work fits", "implementation plans fit Plan, and rendered UI work fits")
    if revision is not None:
        agent["config"]["builtin_defaults"] = _record(agent, revision)
    if migrated:
        from app.core.tool_access import fold_legacy_tool_switches
        agent["config"] = fold_legacy_tool_switches(agent["config"])
    assert is_untouched_builtin(agent)
    database.update_profile(role, description=agent["description"], config=agent["config"], allow_builtin=True)
    assert seed_builtin_subagents()[role] == "retired"
    assert database.get_profile(role) is None


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("edit", EDITS)
def test_customized_builtin_keeps_identity_and_references(mock_db, role, edit):
    from app.db import turns, wakeups

    agent = create_unchanged_builtin(role)
    config = copy.deepcopy(agent["config"])
    config["builtin_defaults"] = _record(agent)
    changes = {"config": config}
    if edit in ("name", "description", "avatar_color"):
        changes[edit] = "violet" if edit == "avatar_color" else "Owner's " + edit
    elif edit == "mcp_tools":
        changes[edit] = ["mcp__owner__tool"]
    elif edit == "prompt":
        config["system_prompt"]["content"] = "Owner instructions"
    elif edit == "permissions":
        rule = database.add_permission_rule(role, "Bash", "*", "deny")
    elif edit == "unmatchable":
        config = changes["config"] = {"unknown_schema": {"instructions": "Owner instructions"}}
    elif edit == "future_record":
        config["builtin_defaults"]["revision"] = 999
    else:
        config.update({
            "model": {"model": "owner-model"}, "tools": {"allowed_tools": ["Read"]},
            "provider_models": {"provider_models": {"__native__": "owner-model"}},
            "memory": {"memory": "Owner memory"}, "skills": {"skills": ["owner-skill"]},
            "mcp_servers": {"mcp_servers": ["owner-server"]},
            "enabled_agents": {"enabled_agents": ["owner-helper"]},
            "unknown": {"future_owner_setting": True},
        }[edit])
    database.update_profile(role, allow_builtin=True, **changes)
    before = database.get_profile(role)
    database.create_session("retirement-chat", role)
    database.create_automation("retirement-job", "Saved job", "interval", {"seconds": 300},
                               role, prompt="Do work", enabled=True)
    database.create_api_user("retirement-only", "Scoped key", "synthetic-preservation", profile_ids=[role])
    wake = wakeups.create_wakeup(id="retirement-wake", session_id="retirement-chat", profile_id=role,
                                prompt="Continue", fire_at=9999999999)
    wake = next(r for r in wakeups.get_pending_wakeups() if r["id"] == wake["id"])
    turn = turns.enqueue_turn("retirement-chat", "user", profile_id=role, prompt="Do work")
    automation = database.get_automation("retirement-job")
    session = database.get_session("retirement-chat")
    seed_builtin_subagents()
    seed_builtin_subagents()
    assert database.get_profile(role) == {**before, "is_builtin": False}
    assert database.get_session("retirement-chat") == session
    assert database.get_automation("retirement-job") == automation
    assert database.get_api_user("retirement-only")["profile_ids"] == [role]
    assert turns.get_turn(turn["id"])["state"] == turn["state"]
    assert next(r for r in wakeups.get_pending_wakeups() if r["id"] == wake["id"]) == wake
    if edit == "permissions":
        assert database.get_permission_rules(role) == [rule]


@pytest.mark.parametrize("role", ROLES)
def test_preserved_renamed_role_satisfies_template_without_overwrite(mock_db, role):
    from app.core.agent_templates import install_starter_team, list_agent_templates

    create_unchanged_builtin(role)
    database.update_profile(role, name="My renamed role", config={"model": "owner-model"}, allow_builtin=True)
    seed_builtin_subagents()
    preserved = database.get_profile(role)
    assert next(m for m in list_agent_templates()[0]["agents"] if m["key"] == role)["existing_agent_id"] == role
    installed = install_starter_team()
    try:
        members = {m["key"]: m["existing_agent_id"] for m in installed["agents"]}
        assert members[role] == role
        assert members[next(r for r in ROLES if r != role)] not in ROLES
        assert install_starter_team() == installed
        assert database.get_profile(role) == preserved
        assert "starter_team_role" not in preserved["config"]
    finally:
        for member in installed["agents"]:
            if member["existing_agent_id"] != role:
                database.delete_profile(member["existing_agent_id"])


@pytest.mark.parametrize("role", ROLES)
def test_old_builtin_snapshot_cannot_overwrite_preserved_role(mock_db, role):
    from app.network import engine, policy

    old = create_unchanged_builtin(role)
    database.update_profile(role, name="My renamed role", allow_builtin=True)
    seed_builtin_subagents()
    preserved = database.get_profile(role)
    session = database.create_session("retirement-chat", role)
    automation = database.create_automation("retirement-job", "Saved job", "interval", {"seconds": 300},
                                           role, prompt="Do work", enabled=True)
    with database.get_db() as conn:
        cursor = conn.cursor()
        engine.materialize(cursor, policy.collection("agents"), role, old)
        engine.materialize(cursor, policy.collection("sessions"), session["id"], session)
        coll = policy.collection("automations")
        # Use the real sync producer, not the API's decoded action/trigger lists.
        wire = engine.serialize_row(coll, engine._read_row(cursor, coll, automation["id"]))
        engine.materialize(cursor, coll, automation["id"], wire)
    assert database.get_profile(role) == preserved
    assert database.get_session(session["id"])["profile_id"] == role
    assert database.get_automation(automation["id"])["profile_id"] == role
    assert database.get_automation(automation["id"])["is_enabled"]
    assert old["is_builtin"]  # The signed incoming record was not rewritten.
