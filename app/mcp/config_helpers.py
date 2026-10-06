"""
Shared helper functions and constants for app-config SDK tools.

Provides result formatting, secret sanitization, summary builders,
and shared constants used across all config tool modules.
"""

import json
from datetime import date, datetime
from typing import Any

from app.core.models import VALID_HOOK_EVENTS


# ============================================================================
# Constants
# ============================================================================

VALID_HOOK_TYPES = ["bash", "webhook"]

VALID_TRIGGER_TYPES = ["cron", "interval", "webhook"]


def _json_default(obj: Any) -> str:
    """Fallback serializer for types stdlib json can't encode natively.

    The DB layer (psycopg) returns real datetime/date objects, which json.dumps
    rejects. ISO-format those to match the rest of the API; degrade anything
    else to its string form so a stray type can never crash a read tool.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _tool_result(result: dict) -> dict:
    """Wrap a result dict in the MCP tool response format."""
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2, default=_json_default)}]}


def _error(message: str) -> dict:
    """Return an error result."""
    return _tool_result({"success": False, "error": message})


def _sanitize_hook(hook: dict) -> dict:
    """Strip secrets from hook data before returning to the agent."""
    safe = dict(hook)
    if safe.get("secret"):
        safe["secret"] = "••••••••"
    return safe


def _sanitize_automation(auto: dict) -> dict:
    """Strip secrets from automation data before returning to the agent.

    Copies every level it changes, so the caller's dict is never mutated.
    The stored trigger shape is ``{"type", "config": {"secret", ...}}``; the
    flat ``triggers[i].secret`` form is masked too for older rows.
    """
    safe = dict(auto)
    # Mask secrets in trigger_config
    tc = safe.get("trigger_config")
    if tc and isinstance(tc, dict) and tc.get("secret"):
        tc = dict(tc)
        tc["secret"] = "••••••••"
        safe["trigger_config"] = tc
    # Mask secrets in triggers array
    triggers = safe.get("triggers")
    if triggers and isinstance(triggers, list):
        safe_triggers = []
        for t in triggers:
            if not isinstance(t, dict):
                safe_triggers.append(t)
                continue
            t = dict(t)
            if t.get("secret"):
                t["secret"] = "••••••••"
            config = t.get("config")
            if isinstance(config, dict) and config.get("secret"):
                config = dict(config)
                config["secret"] = "••••••••"
                t["config"] = config
            safe_triggers.append(t)
        safe["triggers"] = safe_triggers
    return safe


def _sanitize_connector(conn: dict) -> dict:
    """Strip secrets from connector config."""
    safe = dict(conn)
    config = safe.get("config")
    if config and isinstance(config, dict):
        masked = {}
        for k, v in config.items():
            if any(secret_word in k.lower() for secret_word in
                   ("token", "secret", "key", "password", "credential")):
                masked[k] = "••••••••" if v else None
            else:
                masked[k] = v
        safe["config"] = masked
    return safe


def _hook_summary(hook: dict) -> dict:
    """Return a compact summary of a hook for list views."""
    return {
        "id": hook.get("id"),
        "name": hook.get("name"),
        "event": hook.get("event"),
        "hook_type": hook.get("hook_type"),
        "enabled": hook.get("enabled"),
        "tool_matcher": hook.get("tool_matcher"),
        "scope": "project" if hook.get("project_id") else "app",
        "last_triggered_at": hook.get("last_triggered_at"),
        "failure_count": hook.get("failure_count", 0),
    }


def _automation_summary(auto: dict) -> dict:
    """Return a compact summary of an automation for list views."""
    return {
        "id": auto.get("id"),
        "name": auto.get("name"),
        "description": auto.get("description"),
        "trigger_type": auto.get("trigger_type"),
        "is_enabled": auto.get("is_enabled"),
        "status": auto.get("status"),
        "last_run_at": auto.get("last_run_at"),
        "run_count": auto.get("run_count", 0),
        "failure_count": auto.get("failure_count", 0),
        "total_cost_used": auto.get("total_cost_used", 0),
        "requires_approval": auto.get("requires_approval", False),
        "model": auto.get("model"),
    }
