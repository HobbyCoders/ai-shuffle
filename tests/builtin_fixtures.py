"""Real shipped rows for retirement tests (not arbitrary/customized built-ins)."""
from app.core.agent_templates import STARTER_TEAM_AGENTS
from app.db import database


def create_unchanged_builtin(agent_id):
    definition = next((row for row in STARTER_TEAM_AGENTS if row["id"] == agent_id), None)
    if definition is None:  # Explore and Plan retain their prior unconditional retirement.
        return database.create_profile(agent_id, agent_id, None, {}, is_builtin=True)
    database.create_subagent(
        subagent_id=agent_id, name=definition["name"], description=definition["description"],
        prompt=definition["prompt"], tools=definition["tools"], model=definition["model"],
        is_builtin=True,
    )
    return database.get_profile(agent_id)
