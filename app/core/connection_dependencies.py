"""References from automations to persistent connector instances.

Package/UI changes must not turn a connection id into a plugin id. This module
is deliberately independent of plugin installation and connector execution.
"""
from typing import Any

from app.db import database


def connection_dependencies(connector_id: str, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    result = []
    for automation in database.get_all_automations():
        if enabled_only and not automation.get("is_enabled"):
            continue
        uses = []
        triggers = automation.get("triggers") or [{
            "type": automation.get("trigger_type"),
            "config": automation.get("trigger_config") or {},
        }]
        for trigger in triggers:
            config = trigger.get("config") or {}
            if trigger.get("type") not in ("connector_event", "connector_trigger"):
                continue
            # An unspecified connection means ALL connections, not no dependency.
            target = config.get("connector_id") or (config.get("filter") or {}).get("connector_id")
            if not target or target == connector_id:
                uses.append("trigger")
        for step in automation.get("action_steps") or []:
            if step.get("type") == "connector_action" and step.get("connector_id") == connector_id:
                uses.append("action")
        output = automation.get("output_config") or {}
        if output.get("type") == "connector" and output.get("connector_id") == connector_id:
            uses.append("output")
        if uses:
            result.append({"id": automation["id"], "name": automation.get("name", automation["id"]),
                           "is_enabled": bool(automation.get("is_enabled")), "uses": sorted(set(uses))})
    return result


def require_unreferenced_connection(connector_id: str, *, enabled_only: bool = False) -> None:
    dependencies = connection_dependencies(connector_id, enabled_only=enabled_only)
    if dependencies:
        names = ", ".join(item["name"] for item in dependencies)
        raise ValueError(f"This connection is used by automations: {names}. Update those automations first.")
