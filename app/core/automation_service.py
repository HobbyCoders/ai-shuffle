"""
Automation service — business logic for CRUD, budget checking, and prompt rendering.
"""

import hashlib
import logging
import re
import secrets
from typing import Any, Dict, List, Optional, Tuple

from app.core import encryption
from app.core.models import VALID_AUTOMATION_TRIGGER_TYPES
from app.db import database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt template rendering
# ---------------------------------------------------------------------------

def render_prompt(template: str, trigger_data: Optional[Dict[str, Any]] = None) -> str:
    """Render a prompt template by substituting ``{{variable}}`` placeholders.

    Supports nested access via dot notation: ``{{event.session_id}}``.
    Missing variables are left as-is (no error).
    """
    if not trigger_data:
        return template

    def _resolve(match: re.Match) -> str:
        key = match.group(1).strip()
        parts = key.split(".")
        value: Any = trigger_data
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return match.group(0)  # leave unresolved
            if value is None:
                return match.group(0)
        return str(value)

    return re.sub(r"\{\{(.+?)\}\}", _resolve, template)


def render_action_params(params: Dict[str, Any], trigger_data: Dict, response: str = "") -> Dict[str, Any]:
    """Render template variables in action step parameters.

    Supports ``{{trigger.data.xxx}}`` and ``{{response}}`` templates.
    Non-string values are passed through unchanged.
    """
    rendered = {}
    for key, value in params.items():
        if isinstance(value, str):
            rendered[key] = render_prompt(value, {**trigger_data, "response": response})
        else:
            rendered[key] = value
    return rendered


# ---------------------------------------------------------------------------
# Budget checking
# ---------------------------------------------------------------------------

def check_budget(automation: Dict[str, Any]) -> Tuple[bool, str]:
    """Check whether an automation is within its budget limits.

    Returns (ok, reason).  If *ok* is False, *reason* explains which limit
    was hit.
    """
    automation_id = automation["id"]

    # Lifetime cap
    max_total = automation.get("max_cost_total")
    if max_total is not None and automation.get("total_cost_used", 0) >= max_total:
        return False, f"Lifetime budget exceeded (${automation['total_cost_used']:.4f} / ${max_total:.4f})"

    # Daily limits
    daily = database.get_automation_daily_stats(automation_id)

    max_runs = automation.get("max_runs_per_day")
    if max_runs is not None and daily["run_count"] >= max_runs:
        return False, f"Daily run limit reached ({daily['run_count']} / {max_runs})"

    max_day_cost = automation.get("max_cost_per_day")
    if max_day_cost is not None and daily["total_cost"] >= max_day_cost:
        return False, f"Daily cost limit reached (${daily['total_cost']:.4f} / ${max_day_cost:.4f})"

    return True, "OK"


# ---------------------------------------------------------------------------
# Webhook secret encryption helpers
# ---------------------------------------------------------------------------

def encrypt_webhook_secret(trigger_config: Dict[str, Any]) -> Dict[str, Any]:
    """Encrypt the webhook secret in trigger_config if present."""
    config = dict(trigger_config)
    secret = config.get("secret")
    if secret and isinstance(secret, str) and not encryption.is_encrypted(secret):
        if encryption.is_encryption_ready():
            try:
                config["secret"] = encryption.encrypt_value(secret)
            except Exception as e:
                logger.error(f"Failed to encrypt webhook secret: {e}")
    return config


def decrypt_webhook_secret(trigger_config: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypt the webhook secret in trigger_config if present."""
    config = dict(trigger_config)
    secret = config.get("secret")
    if secret and isinstance(secret, str) and encryption.is_encrypted(secret):
        if encryption.is_encryption_ready():
            try:
                config["secret"] = encryption.decrypt_value(secret)
            except Exception as e:
                logger.error(f"Failed to decrypt webhook secret: {e}")
    return config


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

async def create_automation(
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
) -> Dict:
    """Create a new automation.

    Returns the automation dict.
    """
    # Validate trigger type
    if trigger_type not in VALID_AUTOMATION_TRIGGER_TYPES:
        raise ValueError(f"Invalid trigger type: {trigger_type}")

    # Validate profile exists
    profile = database.get_profile(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Validate project exists if specified
    if project_id:
        project = database.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Validate interval minimum
    if trigger_type == "interval":
        interval_seconds = trigger_config.get("seconds", 0)
        if interval_seconds < 60:
            raise ValueError("Interval must be at least 60 seconds")

    # Generate automation ID
    automation_id = f"auto-{secrets.token_hex(4)}"

    # Encrypt webhook secret if applicable
    if trigger_type == "webhook":
        # Auto-generate a secret if not provided
        if not trigger_config.get("secret"):
            trigger_config["secret"] = secrets.token_urlsafe(32)
        trigger_config = encrypt_webhook_secret(trigger_config)

    automation = database.create_automation(
        automation_id=automation_id,
        name=name,
        trigger_type=trigger_type,
        trigger_config=trigger_config,
        prompt=prompt,
        profile_id=profile_id,
        project_id=project_id,
        description=description,
        output_config=output_config,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        max_cost_per_run=max_cost_per_run,
        max_cost_per_day=max_cost_per_day,
        max_cost_total=max_cost_total,
        max_runs_per_day=max_runs_per_day,
        requires_approval=requires_approval,
        action_steps=action_steps,
    )

    if not automation:
        raise RuntimeError("Failed to create automation in database")

    return automation


async def update_automation(automation_id: str, **kwargs) -> Optional[Dict]:
    """Update an existing automation.

    If trigger config changes and the automation is enabled, it will be rescheduled.
    """
    existing = database.get_automation(automation_id)
    if not existing:
        return None

    # Validate profile if being changed
    if "profile_id" in kwargs and kwargs["profile_id"]:
        profile = database.get_profile(kwargs["profile_id"])
        if not profile:
            raise ValueError(f"Profile not found: {kwargs['profile_id']}")

    # Validate project if being changed
    if "project_id" in kwargs and kwargs["project_id"]:
        project = database.get_project(kwargs["project_id"])
        if not project:
            raise ValueError(f"Project not found: {kwargs['project_id']}")

    # Encrypt webhook secret if trigger config is being updated
    trigger_changed = False
    if "trigger_config" in kwargs and kwargs["trigger_config"] is not None:
        trigger_type = kwargs.get("trigger_type", existing["trigger_type"])
        if trigger_type == "webhook":
            kwargs["trigger_config"] = encrypt_webhook_secret(kwargs["trigger_config"])
        trigger_changed = True

    if "trigger_type" in kwargs:
        trigger_changed = True

    updated = database.update_automation(automation_id, **kwargs)
    if not updated:
        return None

    # Reschedule if the automation is enabled and trigger changed
    if trigger_changed and updated.get("is_enabled"):
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.schedule_automation(automation_id)
        except Exception as e:
            logger.error(f"Failed to reschedule automation {automation_id}: {e}")

    return updated


async def delete_automation(automation_id: str) -> bool:
    """Delete an automation, stop it if running."""
    automation = database.get_automation(automation_id)
    if not automation:
        return False

    # Unschedule if enabled
    if automation.get("is_enabled"):
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.unschedule_automation(automation_id)
        except Exception as e:
            logger.error(f"Error unscheduling automation {automation_id}: {e}")

    return database.delete_automation(automation_id)


async def test_automation(automation_id: str) -> Dict:
    """Dry-run: render prompt and check budget, but do NOT execute.

    Returns ``{"prompt_rendered": str, "budget_ok": bool, "message": str}``.
    """
    automation = database.get_automation(automation_id)
    if not automation:
        return {"prompt_rendered": "", "budget_ok": False, "message": "Automation not found"}

    # Render prompt with empty trigger data to show what it looks like
    rendered = render_prompt(automation.get("prompt") or "", {"event": {}, "payload": {}})

    # Check budget
    ok, reason = check_budget(automation)

    return {
        "prompt_rendered": rendered,
        "budget_ok": ok,
        "message": reason,
    }


def get_automation_stats(automation_id: str) -> Dict:
    """Get statistics for an automation."""
    automation = database.get_automation(automation_id)
    if not automation:
        return {}

    daily = database.get_automation_daily_stats(automation_id)

    # Get all-time stats from runs
    runs = database.get_automation_runs(automation_id, limit=10000)
    successful = sum(1 for r in runs if r.get("status") == "completed")
    failed = sum(1 for r in runs if r.get("status") == "failed")
    total_cost = sum(r.get("cost_usd", 0) for r in runs)
    durations = [r.get("duration_ms", 0) for r in runs if r.get("duration_ms")]
    avg_duration = sum(durations) / len(durations) if durations else 0

    return {
        "automation_id": automation_id,
        "total_runs": len(runs),
        "successful_runs": successful,
        "failed_runs": failed,
        "total_cost_usd": total_cost,
        "avg_duration_ms": avg_duration,
        "today_runs": daily["run_count"],
        "today_cost_usd": daily["total_cost"],
    }
