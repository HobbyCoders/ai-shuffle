"""Automation database operations."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TypedDict

from app.db.base import get_db, row_to_dict, rows_to_list, logger


# ============================================================================
# TypedDict definitions
# ============================================================================

class AutomationRow(TypedDict):
    """Full automation row from SELECT * FROM automations."""
    id: str
    name: str
    description: Optional[str]
    trigger_type: str
    trigger_config: Dict[str, Any]
    triggers: Optional[List[Dict[str, Any]]]
    prompt: str
    profile_id: str
    project_id: Optional[str]
    output_config: Dict[str, Any]
    max_turns: int
    timeout_seconds: int
    max_cost_per_run: Optional[float]
    max_cost_per_day: Optional[float]
    max_cost_total: Optional[float]
    max_runs_per_day: Optional[int]
    total_cost_used: float
    is_enabled: bool
    status: str
    last_run_at: Optional[str]
    last_error: Optional[str]
    run_count: int
    failure_count: int
    requires_approval: bool
    action_steps: Optional[List[Dict[str, Any]]]
    created_at: str
    updated_at: str


class AutomationRunRow(TypedDict):
    """Full automation run row from SELECT * FROM automation_runs."""
    id: str
    automation_id: str
    status: str
    trigger_data: Optional[Dict[str, Any]]
    session_id: Optional[str]
    prompt_rendered: Optional[str]
    response: Optional[str]
    error: Optional[str]
    cost_usd: float
    tokens_in: int
    tokens_out: int
    duration_ms: int
    output_status: str
    output_error: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    created_at: str


class AutomationApprovalRow(TypedDict):
    """Automation approval row from SELECT * FROM automation_approvals."""
    id: str
    run_id: str
    automation_id: str
    status: str
    prompt_preview: Optional[str]
    decided_by: Optional[str]
    decided_at: Optional[str]
    created_at: str


class AutomationApprovalWithName(TypedDict):
    """Pending approval with joined automation name."""
    id: str
    run_id: str
    automation_id: str
    status: str
    prompt_preview: Optional[str]
    decided_by: Optional[str]
    decided_at: Optional[str]
    created_at: str
    automation_name: str


class AutomationDailyStats(TypedDict):
    """Daily stats for an automation."""
    run_count: int
    total_cost: float


def _parse_automation_row(row: Optional[Dict[str, Any]]) -> Optional[AutomationRow]:
    """Parse JSON fields in an automation row."""
    if not row:
        return None
    for field in ("trigger_config", "output_config"):
        if row.get(field) and isinstance(row[field], str):
            row[field] = json.loads(row[field])
    # Deserialize action_steps from JSON string
    if row.get("action_steps") and isinstance(row["action_steps"], str):
        row["action_steps"] = json.loads(row["action_steps"])
    # Deserialize triggers from JSON string
    if row.get("triggers") and isinstance(row["triggers"], str):
        row["triggers"] = json.loads(row["triggers"])
    # Backward compatibility: synthesize triggers from trigger_type/trigger_config if missing
    if not row.get("triggers"):
        row["triggers"] = [{"type": row.get("trigger_type", "cron"), "config": row.get("trigger_config", {})}]
    return row


def _parse_automation_run_row(row: Optional[Dict[str, Any]]) -> Optional[AutomationRunRow]:
    """Parse JSON fields in an automation run row."""
    if not row:
        return None
    if row.get("trigger_data") and isinstance(row["trigger_data"], str):
        row["trigger_data"] = json.loads(row["trigger_data"])
    return row


def create_automation(
    automation_id: str,
    name: str,
    trigger_type: str,
    trigger_config: Dict[str, Any],
    profile_id: str,
    prompt: Optional[str] = None,
    project_id: Optional[str] = None,
    description: Optional[str] = None,
    output_config: Optional[Dict[str, Any]] = None,
    max_turns: int = 1,
    timeout_seconds: int = 300,
    max_cost_per_run: Optional[float] = None,
    max_cost_per_day: Optional[float] = None,
    max_cost_total: Optional[float] = None,
    max_runs_per_day: Optional[int] = None,
    requires_approval: bool = False,
    action_steps: Optional[List[Dict[str, Any]]] = None,
    triggers: Optional[List[Dict[str, Any]]] = None,
) -> Optional[AutomationRow]:
    """Create a new automation."""
    now = datetime.now(timezone.utc).isoformat()
    if output_config is None:
        output_config = {"type": "session"}
    # Build triggers array if not provided
    if not triggers:
        triggers = [{"type": trigger_type, "config": trigger_config}]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO automations
               (id, name, description, trigger_type, trigger_config, prompt,
                profile_id, project_id, output_config, max_turns, timeout_seconds,
                max_cost_per_run, max_cost_per_day, max_cost_total, max_runs_per_day,
                requires_approval, action_steps, triggers, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (automation_id, name, description, trigger_type,
             json.dumps(trigger_config), prompt or "", profile_id, project_id,
             json.dumps(output_config), max_turns, timeout_seconds,
             max_cost_per_run, max_cost_per_day, max_cost_total, max_runs_per_day,
             requires_approval, json.dumps(action_steps) if action_steps else None,
             json.dumps(triggers),
             now, now)
        )
    return get_automation(automation_id)


def get_automation(automation_id: str) -> Optional[AutomationRow]:
    """Get an automation by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automations WHERE id = %s", (automation_id,))
        return _parse_automation_row(row_to_dict(cursor.fetchone()))


def get_all_automations() -> List[AutomationRow]:
    """Get all automations."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automations ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        return [_parse_automation_row(r) for r in rows]


def get_enabled_automations() -> List[AutomationRow]:
    """Get all enabled automations."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automations WHERE is_enabled = TRUE ORDER BY created_at DESC")
        rows = rows_to_list(cursor.fetchall())
        return [_parse_automation_row(r) for r in rows]


def get_automations_for_event(trigger_type: str, event_type: str, connector_id: Optional[str] = None) -> List[AutomationRow]:
    """Get enabled automations matching a connector event or connector trigger.

    Searches the triggers array for any matching trigger.
    """
    automations = get_enabled_automations()
    results = []
    for a in automations:
        triggers = a.get("triggers") or []
        matched = False
        for trig in triggers:
            if trig.get("type") != trigger_type:
                continue
            tc = trig.get("config", {})
            if trigger_type == "connector_event":
                if tc.get("event_type") != event_type:
                    continue
                if connector_id and tc.get("connector_id") and tc["connector_id"] != connector_id:
                    continue
            elif trigger_type == "connector_trigger":
                # Match on trigger_id and optionally connector_id
                if tc.get("trigger_id") != event_type:
                    continue
                if connector_id and tc.get("connector_id") and tc["connector_id"] != connector_id:
                    continue
            matched = True
            break
        if matched:
            results.append(a)
    return results


def update_automation(automation_id: str, **kwargs) -> Optional[AutomationRow]:
    """Update an automation. Pass keyword arguments for fields to update."""
    existing = get_automation(automation_id)
    if not existing:
        return None

    updates = []
    values = []

    for key in ("name", "description", "prompt", "profile_id", "project_id",
                "max_turns", "timeout_seconds", "max_cost_per_run", "max_cost_per_day",
                "max_cost_total", "max_runs_per_day", "is_enabled", "status",
                "last_run_at", "last_error", "run_count", "failure_count",
                "total_cost_used", "requires_approval"):
        if key in kwargs:
            updates.append(f"{key} = %s")
            values.append(kwargs[key])

    for key in ("trigger_config", "output_config"):
        if key in kwargs:
            updates.append(f"{key} = %s")
            values.append(json.dumps(kwargs[key]))

    if "action_steps" in kwargs:
        updates.append("action_steps = %s")
        values.append(json.dumps(kwargs["action_steps"]) if kwargs["action_steps"] is not None else None)

    if "triggers" in kwargs:
        updates.append("triggers = %s")
        values.append(json.dumps(kwargs["triggers"]) if kwargs["triggers"] is not None else None)

    if "trigger_type" in kwargs:
        updates.append("trigger_type = %s")
        values.append(kwargs["trigger_type"])

    if updates:
        updates.append("updated_at = %s")
        values.append(datetime.now(timezone.utc).isoformat())
        values.append(automation_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE automations SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_automation(automation_id)


def delete_automation(automation_id: str) -> bool:
    """Delete an automation (cascades to runs and approvals)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM automations WHERE id = %s", (automation_id,))
        return cursor.rowcount > 0


# ---- Automation Runs ----

def create_automation_run(
    run_id: str,
    automation_id: str,
    status: str = "pending",
    trigger_data: Optional[Dict[str, Any]] = None,
    prompt_rendered: Optional[str] = None,
) -> Optional[AutomationRunRow]:
    """Create a new automation run."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO automation_runs
               (id, automation_id, status, trigger_data, prompt_rendered, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (run_id, automation_id, status,
             json.dumps(trigger_data) if trigger_data else None,
             prompt_rendered, now)
        )
    return get_automation_run(run_id)


def get_automation_run(run_id: str) -> Optional[AutomationRunRow]:
    """Get an automation run by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automation_runs WHERE id = %s", (run_id,))
        return _parse_automation_run_row(row_to_dict(cursor.fetchone()))


def get_automation_runs(automation_id: str, limit: int = 50, offset: int = 0) -> List[AutomationRunRow]:
    """Get runs for an automation, most recent first."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM automation_runs WHERE automation_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (automation_id, limit, offset)
        )
        rows = rows_to_list(cursor.fetchall())
        return [_parse_automation_run_row(r) for r in rows]


def update_automation_run(run_id: str, **kwargs) -> Optional[AutomationRunRow]:
    """Update an automation run."""
    updates = []
    values = []

    for key in ("status", "session_id", "prompt_rendered", "response", "error",
                "cost_usd", "tokens_in", "tokens_out", "duration_ms",
                "output_status", "output_error", "started_at", "completed_at"):
        if key in kwargs:
            updates.append(f"{key} = %s")
            values.append(kwargs[key])

    if "trigger_data" in kwargs:
        updates.append("trigger_data = %s")
        values.append(json.dumps(kwargs["trigger_data"]) if kwargs["trigger_data"] else None)

    if updates:
        values.append(run_id)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE automation_runs SET {', '.join(updates)} WHERE id = %s",
                values
            )

    return get_automation_run(run_id)


def get_automation_daily_stats(automation_id: str) -> AutomationDailyStats:
    """Get today's run count and cost for an automation."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT COUNT(*) as run_count, COALESCE(SUM(cost_usd), 0) as total_cost
               FROM automation_runs
               WHERE automation_id = %s AND created_at >= %s""",
            (automation_id, today)
        )
        row = cursor.fetchone()
        return {"run_count": row["run_count"], "total_cost": row["total_cost"]}


# ---- Automation Approvals ----

def create_automation_approval(
    approval_id: str,
    run_id: str,
    automation_id: str,
    prompt_preview: Optional[str] = None,
) -> Optional[AutomationApprovalRow]:
    """Create a new automation approval."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO automation_approvals
               (id, run_id, automation_id, prompt_preview, created_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (approval_id, run_id, automation_id, prompt_preview, now)
        )
    return get_automation_approval(approval_id)


def get_automation_approval(approval_id: str) -> Optional[AutomationApprovalRow]:
    """Get an automation approval by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM automation_approvals WHERE id = %s", (approval_id,))
        return row_to_dict(cursor.fetchone())


def get_pending_automation_approvals() -> List[AutomationApprovalWithName]:
    """Get all pending automation approvals."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT a.*, au.name as automation_name
               FROM automation_approvals a
               JOIN automations au ON a.automation_id = au.id
               WHERE a.status = 'pending'
               ORDER BY a.created_at DESC"""
        )
        return rows_to_list(cursor.fetchall())


def update_automation_approval(approval_id: str, status: str, decided_by: Optional[str] = None) -> Optional[AutomationApprovalRow]:
    """Update an automation approval status."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE automation_approvals SET status = %s, decided_by = %s, decided_at = %s WHERE id = %s",
            (status, decided_by, now, approval_id)
        )
    return get_automation_approval(approval_id)
