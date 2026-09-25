"""Subagent-shaped compat layer over the unified ``agents`` table + permission rules.

The ``subagents`` table is gone — every former subagent row lives in
``agents`` with its prompt/tools/model/memory folded into the config blob.
Existing callers keep working through this module, which projects agent rows into the old subagent dict
shape and maps writes back onto agent configs.

Built-in subagents' "revert to defaults" now compares against the CODE
definitions in ``app.core.builtin_subagents.BUILTIN_SUBAGENTS`` — the old
``default_*`` snapshot columns died in the fold.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, List, Dict, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list
from app.db import profiles as _agents_db


class SubagentRow(TypedDict):
    """Subagent-shaped projection of an agent row."""
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]]
    model: Optional[str]
    skills: Optional[List[str]]
    memory: Optional[str]
    mcp_servers: Optional[List[Any]]
    is_builtin: bool
    created_at: str
    updated_at: str


class PermissionRuleRow(TypedDict):
    """Full permission rule row from SELECT * FROM permission_rules."""
    id: str
    profile_id: Optional[str]
    tool_name: str
    tool_pattern: Optional[str]
    decision: str
    created_at: str


# ============================================================================
# Agent-row <-> subagent-shape projection
# ============================================================================

def _agent_to_subagent(agent: Optional[dict]) -> Optional[SubagentRow]:
    if not agent:
        return None
    config = agent.get("config") or {}
    sp = config.get("system_prompt") or {}
    prompt = sp.get("content", "") if isinstance(sp, dict) else str(sp or "")
    return {
        "id": agent["id"],
        "name": agent.get("name") or agent["id"],
        "description": agent.get("description") or "",
        "prompt": prompt or "",
        "tools": config.get("allowed_tools"),
        "model": config.get("model"),
        "skills": config.get("skills"),
        "memory": config.get("memory"),
        "mcp_servers": config.get("mcp_servers"),
        "is_builtin": bool(agent.get("is_builtin")),
        "created_at": agent.get("created_at"),
        "updated_at": agent.get("updated_at"),
    }


def _subagent_config(
    prompt: str,
    tools: Optional[List[str]],
    model: Optional[str],
    skills: Optional[List[str]],
    memory: Optional[str],
    mcp_servers: Optional[List[Any]],
    base: Optional[dict] = None,
) -> dict:
    config = dict(base or {})
    config["system_prompt"] = {"type": "custom", "content": prompt or ""}
    if tools is not None:
        config["allowed_tools"] = tools
    else:
        config.pop("allowed_tools", None)
    if model:
        config["model"] = model
    else:
        config.pop("model", None)
    if skills is not None:
        config["skills"] = skills
    else:
        config.pop("skills", None)
    if memory is not None:
        config["memory"] = memory
    else:
        config.pop("memory", None)
    if mcp_servers is not None:
        config["mcp_servers"] = mcp_servers
    else:
        config.pop("mcp_servers", None)
    return config


def _code_defaults(subagent_id: str) -> Optional[Dict[str, Any]]:
    """The in-repo definition for a built-in subagent (revert source)."""
    from app.core.builtin_subagents import BUILTIN_SUBAGENTS

    for definition in BUILTIN_SUBAGENTS:
        if definition.get("id") == subagent_id:
            return definition
    return None


# ============================================================================
# Subagent Operations (over agent rows)
# ============================================================================

def get_subagent(subagent_id: str) -> Optional[SubagentRow]:
    """Get a subagent-shaped view of an agent by ID"""
    return _agent_to_subagent(_agents_db.get_profile(subagent_id))


def get_all_subagents() -> List[SubagentRow]:
    """All agents, subagent-shaped (unified entity: any agent is a target)."""
    return [_agent_to_subagent(a) for a in _agents_db.get_all_profiles()]


def create_subagent(
    subagent_id: str,
    name: str,
    description: str,
    prompt: str,
    tools: Optional[List[str]] = None,
    model: Optional[str] = None,
    skills: Optional[List[str]] = None,
    memory: Optional[str] = None,
    mcp_servers: Optional[List[Any]] = None,
    is_builtin: bool = False,
    store_defaults: bool = False,
) -> SubagentRow:
    """Create an agent row from subagent-shaped fields.

    ``store_defaults`` is accepted for signature compatibility and ignored —
    built-in defaults live in code (``BUILTIN_SUBAGENTS``) since v71.
    """
    config = _subagent_config(prompt, tools, model, skills, memory, mcp_servers)
    _agents_db.create_profile(
        subagent_id, name, description, config, is_builtin=is_builtin
    )
    return get_subagent(subagent_id)


def update_subagent(
    subagent_id: str,
    name: Optional[str] = ...,  # type: ignore[assignment]
    description: Optional[str] = ...,  # type: ignore[assignment]
    prompt: Optional[str] = ...,  # type: ignore[assignment]
    tools: Optional[List[str]] = ...,  # type: ignore[assignment]
    model: Optional[str] = ...,  # type: ignore[assignment]
    skills: Optional[List[str]] = ...,  # type: ignore[assignment]
    memory: Optional[str] = ...,  # type: ignore[assignment]
    mcp_servers: Optional[List[Any]] = ...,  # type: ignore[assignment]
) -> Optional[SubagentRow]:
    """Update the agent row behind a subagent-shaped view.

    Uses Ellipsis (...) to distinguish "not provided" from "explicitly None".
    """
    agent = _agents_db.get_profile(subagent_id)
    if not agent:
        return None
    current = _agent_to_subagent(agent)
    config = _subagent_config(
        prompt=current["prompt"] if prompt is ... else (prompt or ""),
        tools=current["tools"] if tools is ... else tools,
        model=current["model"] if model is ... else model,
        skills=current["skills"] if skills is ... else skills,
        memory=current["memory"] if memory is ... else memory,
        mcp_servers=current["mcp_servers"] if mcp_servers is ... else mcp_servers,
        base=agent.get("config"),
    )
    _agents_db.update_profile(
        subagent_id,
        name=None if name is ... else name,
        description=None if description is ... else description,
        config=config,
        allow_builtin=True,
    )
    return get_subagent(subagent_id)


def delete_subagent(subagent_id: str) -> bool:
    """Delete the agent row behind a subagent-shaped view"""
    return _agents_db.delete_profile(subagent_id)


def set_subagent_builtin(subagent_id: str, is_builtin: bool) -> bool:
    """Set the is_builtin flag on the agent row"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE agents SET is_builtin = %s, updated_at = %s WHERE id = %s",
            (is_builtin, datetime.now(timezone.utc).isoformat(), subagent_id)
        )
        return cursor.rowcount > 0


def revert_subagent_to_defaults(subagent_id: str) -> Optional[SubagentRow]:
    """Revert a built-in agent to its in-repo definition (BUILTIN_SUBAGENTS)."""
    current = get_subagent(subagent_id)
    if not current or not current.get("is_builtin"):
        return None
    defaults = _code_defaults(subagent_id)
    if not defaults:
        return None
    return update_subagent(
        subagent_id,
        name=defaults.get("name"),
        description=defaults.get("description"),
        prompt=defaults.get("prompt"),
        tools=defaults.get("tools"),
        model=defaults.get("model"),
    )


def builtin_subagent_is_modified(agent: Optional[dict]) -> Optional[bool]:
    """Compare an agent row (or subagent view) against its in-repo definition.

    Pure: no queries, so list endpoints can call it per row. Returns ``None``
    when the row is not a built-in with a code definition, otherwise whether
    name, description, prompt, tools, or model differ. Skills, memory, and
    MCP servers are user-owned extras and never count as modifications.
    """
    if not agent or not agent.get("is_builtin"):
        return None
    defaults = _code_defaults(agent["id"])
    if not defaults:
        return None
    current = agent if "prompt" in agent else _agent_to_subagent(agent)
    if not current:
        return None
    return (
        current["name"] != defaults.get("name")
        or current["description"] != (defaults.get("description") or "")
        or current["prompt"] != (defaults.get("prompt") or "")
        or (current["tools"] or None) != (defaults.get("tools") or None)
        or (current["model"] or None) != (defaults.get("model") or None)
    )


# ============================================================================
# Permission Rules Operations
# ============================================================================

def add_permission_rule(
    profile_id: str,
    tool_name: str,
    tool_pattern: Optional[str],
    decision: str
) -> PermissionRuleRow:
    """Add a permission rule for a profile"""
    import uuid
    rule_id = f"rule-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO permission_rules (id, profile_id, tool_name, tool_pattern, decision, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (rule_id, profile_id, tool_name, tool_pattern, decision, now)
        )
    return get_permission_rule(rule_id)


def get_permission_rule(rule_id: str) -> Optional[PermissionRuleRow]:
    """Get a permission rule by ID"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM permission_rules WHERE id = %s", (rule_id,))
        return row_to_dict(cursor.fetchone())


def get_permission_rules(
    profile_id: Optional[str] = None,
    tool_name: Optional[str] = None
) -> List[PermissionRuleRow]:
    """Get permission rules with optional filters"""
    query = "SELECT * FROM permission_rules WHERE 1=1"
    params = []

    if profile_id:
        query += " AND profile_id = %s"
        params.append(profile_id)
    if tool_name:
        query += " AND tool_name = %s"
        params.append(tool_name)

    query += " ORDER BY created_at DESC"

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return rows_to_list(cursor.fetchall())


def delete_permission_rule(rule_id: str) -> bool:
    """Delete a permission rule"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM permission_rules WHERE id = %s", (rule_id,))
        return cursor.rowcount > 0


def delete_profile_permission_rules(profile_id: str) -> int:
    """Delete all permission rules for a profile"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM permission_rules WHERE profile_id = %s", (profile_id,))
        return cursor.rowcount


from app.core.resource_sync import install_database_mutations as _install_resource_sync

_install_resource_sync(
    globals(), 'agents',
    'create_subagent update_subagent delete_subagent set_subagent_builtin revert_subagent_to_defaults add_permission_rule delete_permission_rule delete_profile_permission_rules',
    scope='shared',
)
