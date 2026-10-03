"""
Automation management tools for the app-config SDK MCP server.

Provides CRUD operations for AI Shuffle automations (cron, interval,
webhook-triggered Claude sessions).
"""

import logging
import secrets as _secrets
from typing import Any, Dict, Optional

from app.mcp.config_helpers import (
    VALID_TRIGGER_TYPES,
    _tool_result,
    _error,
    _sanitize_automation,
    _automation_summary,
)

logger = logging.getLogger(__name__)


def _describe_model(automation: Dict[str, Any]) -> Optional[str]:
    """The model its runs use, in one line; ``None`` if it cannot be read.

    Blocking — call through ``run_sync``.
    """
    from app.core.automation_service import describe_automation_model

    try:
        return describe_automation_model(automation)
    except Exception:  # noqa: BLE001 — a report only; the save already landed
        logger.debug("Could not describe the automation's model", exc_info=True)
        return None


def _create_automation_tools(is_admin: bool, project_id: Optional[str]) -> list:
    """Create automation management tools."""
    from app.mcp.inproc import tool
    from app.db import database
    from app.db.async_database import run_sync

    _is_admin = is_admin
    _project_id = project_id
    tools = []

    # -- listAutomations --------------------------------------------------

    @tool(
        "listAutomations",
        "List all automations with their status, schedule, and run stats.",
        {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["idle", "running", "paused", "error"],
                    "description": "Filter by status.",
                },
                "enabled_only": {
                    "type": "boolean",
                    "description": "If true, only return enabled automations.",
                },
            },
        },
    )
    async def list_automations(args: dict) -> dict:
        automations = await run_sync(database.get_all_automations)

        if args.get("status"):
            automations = [a for a in automations if a.get("status") == args["status"]]
        if args.get("enabled_only"):
            automations = [a for a in automations if a.get("is_enabled")]

        return _tool_result({
            "automations": [_automation_summary(a) for a in automations],
            "count": len(automations),
        })

    tools.append(list_automations)

    # -- getAutomation ----------------------------------------------------

    @tool(
        "getAutomation",
        "Get full details of a specific automation including trigger config, budget, and recent runs.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
            },
            "required": ["automation_id"],
        },
    )
    async def get_automation(args: dict) -> dict:
        def _load():
            """Row + its recent runs in ONE hop.

            The runs read still only happens when the automation exists, so the
            query count is unchanged; it just no longer costs two separate
            stalls of the event loop mid-turn.
            """
            from app.core.automation_service import with_model_resolution

            auto = database.get_automation(args["automation_id"])
            if not auto:
                return None, []
            # Effective model + whether it is the automation's own choice or
            # the agent/app fallback, as the REST read reports it.
            return with_model_resolution(auto), database.get_automation_runs(args["automation_id"], limit=5)

        auto, runs = await run_sync(_load)
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        sanitized = _sanitize_automation(auto)

        # Include recent runs summary
        sanitized["recent_runs"] = [
            {
                "id": r.get("id"),
                "status": r.get("status"),
                "cost_usd": r.get("cost_usd", 0),
                "duration_ms": r.get("duration_ms", 0),
                "started_at": r.get("started_at"),
                "completed_at": r.get("completed_at"),
                "error": r.get("error"),
            }
            for r in runs
        ]

        return _tool_result(sanitized)

    tools.append(get_automation)

    # -- createAutomation -------------------------------------------------

    @tool(
        "createAutomation",
        "Create a scheduled automation that runs a Claude prompt on a cron schedule, "
        "at fixed intervals, or via webhook trigger. Always set budget limits. "
        "New automations are created ENABLED by default and begin firing on their "
        "schedule immediately — pass enabled=false to create it paused (then call "
        "toggleAutomation to activate it later).",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Automation name."},
                "description": {"type": "string", "description": "What this automation does."},
                "trigger_type": {
                    "type": "string",
                    "enum": VALID_TRIGGER_TYPES,
                    "description": "How the automation is triggered.",
                },
                "cron_expression": {
                    "type": "string",
                    "description": "Cron expression for 'cron' trigger (e.g., '0 9 * * 1-5').",
                },
                "cron_timezone": {
                    "type": "string",
                    "description": "Timezone for cron (default 'UTC'). E.g., 'America/New_York'.",
                },
                "interval_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "description": "Seconds between runs for 'interval' trigger (minimum 60).",
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to send to Claude when triggered. Supports {{variable}} template syntax.",
                },
                "profile_id": {
                    "type": "string",
                    "description": "Profile to use for execution. Use listProfiles to find available profiles.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Project scope. Uses current project if not specified.",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Maximum conversation turns per run (default 1).",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds per run (default 300).",
                },
                "max_cost_per_run": {
                    "type": "number",
                    "description": "Maximum cost in USD per run (strongly recommended).",
                },
                "max_cost_per_day": {
                    "type": "number",
                    "description": "Maximum daily cost in USD.",
                },
                "max_runs_per_day": {
                    "type": "integer",
                    "description": "Maximum number of runs per day.",
                },
                "requires_approval": {
                    "type": "boolean",
                    "description": "If true, each run requires manual approval before executing.",
                },
                "model": {
                    "type": "string",
                    "description": "Model its runs use: a Claude model (e.g. 'sonnet', 'opus') or "
                                   "'prov:<providerId>:<modelId>' for a provider model. With "
                                   "provider_id, a bare model id from that provider's list. "
                                   "Omit to use the app default, the model a new chat uses.",
                },
                "provider_id": {
                    "type": "string",
                    "description": "Provider for 'model' when it is a bare model id. Omit for Claude.",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "Whether the automation is active on creation (default true). "
                                   "When true it starts firing on its schedule right away; set false "
                                   "to create it paused.",
                },
            },
            "required": ["name", "trigger_type", "prompt", "profile_id"],
        },
    )
    async def create_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        trigger_type = args["trigger_type"]

        report: Dict[str, Any] = {}

        def _validate_and_create():
            """Profile lookup → trigger validation → INSERT, in ONE hop.

            The profile read and the insert that depends on it stay in a single
            closure: split across hops another tool call could delete the
            profile in between, and the insert would reference a row this tool
            just proved existed. Validation order is unchanged (profile first,
            then trigger config), so the error a caller sees for a bad request
            is exactly the one it saw before.

            Returns ``(error_message, created_row, automation_id,
            max_cost_per_run)`` — errors are values, never swallowed
            exceptions.
            """
            # Validate profile exists
            profile = database.get_profile(args["profile_id"])
            if not profile:
                return (
                    f"Profile '{args['profile_id']}' not found. "
                    "Use listProfiles to see available profiles."
                ), None, None, None

            # Build trigger config
            trigger_config: Dict[str, Any] = {}
            if trigger_type == "cron":
                if not args.get("cron_expression"):
                    return "Cron automations require 'cron_expression' (e.g., '0 9 * * 1-5').", None, None, None
                trigger_config = {
                    "expression": args["cron_expression"],
                    "timezone": args.get("cron_timezone", "UTC"),
                }
            elif trigger_type == "interval":
                if not args.get("interval_seconds"):
                    return "Interval automations require 'interval_seconds' (minimum 60).", None, None, None
                if args["interval_seconds"] < 60:
                    return "Interval must be at least 60 seconds.", None, None, None
                trigger_config = {"seconds": args["interval_seconds"]}
            elif trigger_type == "webhook":
                trigger_config = {"secret": _secrets.token_urlsafe(32)}

            from app.core.automation_service import normalize_model_choice

            try:
                model, provider_id = normalize_model_choice(
                    args.get("model"), args.get("provider_id")
                )
            except ValueError as e:
                return str(e), None, None, None

            # Enforce budget defaults if not provided
            max_cost = args.get("max_cost_per_run")
            if max_cost is None:
                max_cost = 0.50  # Safe default

            # Default enabled=True: an agent creating an automation intends it
            # to run. Callers can pass enabled=false to create it paused.
            enabled = args.get("enabled", True)

            auto_id = f"auto-{_secrets.token_hex(4)}"
            proj_id = args.get("project_id") or _project_id

            created = database.create_automation(
                automation_id=auto_id,
                name=args["name"],
                description=args.get("description"),
                trigger_type=trigger_type,
                trigger_config=trigger_config,
                prompt=args["prompt"],
                profile_id=args["profile_id"],
                project_id=proj_id,
                max_turns=args.get("max_turns", 1),
                timeout_seconds=args.get("timeout_seconds", 300),
                max_cost_per_run=max_cost,
                max_cost_per_day=args.get("max_cost_per_day"),
                max_runs_per_day=args.get("max_runs_per_day"),
                requires_approval=args.get("requires_approval", False),
                enabled=enabled,
                model=model,
                provider_id=provider_id,
            )
            # The model report reads the agent and provider: same hop.
            if created:
                report["runs_on"] = _describe_model(created)
            return None, created, auto_id, max_cost

        error, auto, automation_id, max_cost_per_run = await run_sync(_validate_and_create)
        if error:
            return _error(error)

        if not auto:
            return _error("Failed to create automation — database error.")

        # Reflect the REAL persisted state, never the requested one.
        is_enabled = bool(auto.get("is_enabled"))
        is_scheduled_trigger = trigger_type in ("cron", "interval")

        # Only register a scheduler job when the automation is actually enabled.
        # schedule_automation() itself no-ops for a disabled automation, so this
        # can never claim a disabled automation is scheduled.
        scheduled = False
        if is_enabled and is_scheduled_trigger:
            try:
                from app.core.automation_manager import automation_manager
                await automation_manager.schedule_automation(automation_id)
                scheduled = True
            except Exception as e:
                logger.warning(f"Automation created but scheduling failed: {e}")

        result: Dict[str, Any] = {
            "success": True,
            "automation_id": automation_id,
            "name": args["name"],
            "trigger_type": trigger_type,
            "enabled": is_enabled,
            "scheduled": scheduled,
            "max_cost_per_run": max_cost_per_run,
            "model": auto.get("model"),
            "runs_on": report.get("runs_on"),
        }

        if trigger_type == "cron":
            result["schedule"] = args["cron_expression"]
            result["timezone"] = args.get("cron_timezone", "UTC")
        elif trigger_type == "interval":
            result["interval_seconds"] = args["interval_seconds"]
        elif trigger_type == "webhook":
            result["webhook_note"] = "Webhook secret has been auto-generated. Use the Automations UI to view the endpoint URL."

        # Honest, state-accurate message.
        if not is_enabled:
            result["message"] = (
                f"Automation '{args['name']}' created DISABLED — it will NOT run until you "
                f"call toggleAutomation(automation_id='{automation_id}', enabled=true). "
                "Visible in the Automations panel."
            )
        elif trigger_type == "cron":
            result["message"] = (
                f"Automation '{args['name']}' created and ACTIVE — next fire per cron "
                f"'{args['cron_expression']}' ({args.get('cron_timezone', 'UTC')}). "
                "Visible in the Automations panel."
            )
        elif trigger_type == "interval":
            result["message"] = (
                f"Automation '{args['name']}' created and ACTIVE — fires every "
                f"{args['interval_seconds']}s. Visible in the Automations panel."
            )
        else:  # webhook
            result["message"] = (
                f"Automation '{args['name']}' created and ACTIVE — runs when its webhook "
                "receives a valid signed request. Visible in the Automations panel."
            )

        return _tool_result(result)

    tools.append(create_automation)

    # -- updateAutomation -------------------------------------------------

    @tool(
        "updateAutomation",
        "Update properties of an existing automation. Pass model='' to clear its "
        "model choice so runs use the app default again.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "prompt": {"type": "string"},
                "profile_id": {"type": "string"},
                "max_turns": {"type": "integer"},
                "timeout_seconds": {"type": "integer"},
                "max_cost_per_run": {"type": "number"},
                "max_cost_per_day": {"type": "number"},
                "max_runs_per_day": {"type": "integer"},
                "requires_approval": {"type": "boolean"},
                "model": {
                    "type": "string",
                    "description": "Model its runs use: a Claude model (e.g. 'sonnet', 'opus') or "
                                   "'prov:<providerId>:<modelId>' for a provider model. With "
                                   "provider_id, a bare model id from that provider's list. "
                                   "Omit to use the app default, the model a new chat uses.",
                },
                "provider_id": {
                    "type": "string",
                    "description": "Provider for 'model' when it is a bare model id. Omit for Claude.",
                },
            },
            "required": ["automation_id"],
        },
    )
    async def update_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        auto_id = args["automation_id"]

        report: Dict[str, Any] = {}

        def _read_validate_write():
            """READ-MODIFY-WRITE — must stay in ONE hop.

            The existence read, the profile validation and the UPDATE are a
            single logical operation. Split across thread hops, two concurrent
            updateAutomation calls could interleave read/read/write/write and
            the second would clobber the first (lost update), or write against
            a row that was deleted between the check and the write.

            Returns ``(error_message, updated_row, kwargs)``.
            """
            existing = database.get_automation(auto_id)
            if not existing:
                return f"Automation not found: {auto_id}", None, None

            # Build update kwargs
            kwargs: Dict[str, Any] = {}
            for field in ("name", "description", "prompt", "profile_id",
                          "max_turns", "timeout_seconds", "max_cost_per_run",
                          "max_cost_per_day", "max_runs_per_day", "requires_approval"):
                if field in args and args[field] is not None:
                    kwargs[field] = args[field]

            # Model choice: '' clears it. A provider alone re-binds the stored
            # model id to that provider (same rule as the REST update).
            if "model" in args or args.get("provider_id"):
                from app.core.automation_service import split_model_selection, normalize_model_choice

                if "model" in args:
                    req_model = args.get("model")
                elif existing.get("model"):
                    req_model = split_model_selection(existing["model"])[0]
                else:
                    req_model = None
                try:
                    kwargs["model"], kwargs["provider_id"] = normalize_model_choice(
                        req_model, args.get("provider_id")
                    )
                except ValueError as e:
                    return str(e), None, None

            if not kwargs:
                return "No fields provided to update.", None, None

            # Validate profile if changing
            if "profile_id" in kwargs:
                profile = database.get_profile(kwargs["profile_id"])
                if not profile:
                    return f"Profile '{kwargs['profile_id']}' not found.", None, None

            updated = database.update_automation(auto_id, **kwargs)
            # The model report reads the agent and provider: same hop.
            if updated:
                report["runs_on"] = _describe_model(updated)
            return None, updated, kwargs

        error, updated, kwargs = await run_sync(_read_validate_write)
        if error:
            return _error(error)
        if not updated:
            return _error("Failed to update automation.")

        return _tool_result({
            "success": True,
            "automation_id": auto_id,
            "updated_fields": list(kwargs.keys()),
            "runs_on": report.get("runs_on"),
            "message": f"Automation '{updated.get('name')}' updated.",
        })

    tools.append(update_automation)

    # -- deleteAutomation -------------------------------------------------

    @tool(
        "deleteAutomation",
        "Delete an automation and all its run history. This cannot be undone.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to confirm deletion.",
                },
            },
            "required": ["automation_id", "confirm"],
        },
    )
    async def delete_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")
        if not args.get("confirm"):
            return _error("Set confirm=true to delete. Ask the user for confirmation first.")

        # Two hops, deliberately: the unschedule between the read and the
        # delete is a coroutine that must run on the caller's loop (it touches
        # the scheduler's loop-bound state), so the read and the write cannot
        # be folded into one closure without changing the order of operations.
        auto = await run_sync(database.get_automation, args["automation_id"])
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        name = auto.get("name", args["automation_id"])

        # Unschedule first
        try:
            from app.core.automation_manager import automation_manager
            await automation_manager.unschedule_automation(args["automation_id"])
        except Exception as e:
            logger.warning(f"Failed to unschedule automation: {e}")

        deleted = await run_sync(database.delete_automation, args["automation_id"])
        if not deleted:
            return _error("Failed to delete automation.")

        return _tool_result({
            "success": True,
            "message": f"Automation '{name}' deleted (including all run history).",
        })

    tools.append(delete_automation)

    # -- toggleAutomation -------------------------------------------------

    @tool(
        "toggleAutomation",
        "Enable or disable an automation without deleting it.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "enabled": {"type": "boolean", "description": "True to enable, false to disable."},
            },
            "required": ["automation_id", "enabled"],
        },
    )
    async def toggle_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        def _read_and_toggle():
            """READ-MODIFY-WRITE — existence check plus the is_enabled flip in
            ONE hop, so a concurrent toggle cannot land between them and leave
            the automation in the state this call already reported."""
            auto = database.get_automation(args["automation_id"])
            if not auto:
                return None, None
            return auto, database.update_automation(
                args["automation_id"], is_enabled=args["enabled"]
            )

        auto, updated = await run_sync(_read_and_toggle)
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        if not updated:
            return _error("Failed to toggle automation.")

        # Update scheduling
        try:
            from app.core.automation_manager import automation_manager
            if args["enabled"]:
                await automation_manager.schedule_automation(args["automation_id"])
            else:
                await automation_manager.unschedule_automation(args["automation_id"])
        except Exception as e:
            logger.warning(f"Schedule update failed: {e}")

        state = "enabled" if args["enabled"] else "disabled"
        return _tool_result({
            "success": True,
            "automation_id": args["automation_id"],
            "enabled": args["enabled"],
            "message": f"Automation '{updated.get('name')}' {state}.",
        })

    tools.append(toggle_automation)

    # -- triggerAutomation ------------------------------------------------

    @tool(
        "triggerAutomation",
        "Manually trigger an automation run right now.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
            },
            "required": ["automation_id"],
        },
    )
    async def trigger_automation(args: dict) -> dict:
        if not _is_admin:
            return _error("App configuration tools require admin access.")

        auto = await run_sync(database.get_automation, args["automation_id"])
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        try:
            from app.core.automation_manager import automation_manager
            result = await automation_manager.trigger_manual(args["automation_id"])
            return _tool_result({
                "success": True,
                "automation_id": args["automation_id"],
                "message": f"Automation '{auto.get('name')}' triggered manually.",
                "run_info": result,
            })
        except Exception as e:
            return _error(f"Failed to trigger automation: {str(e)}")

    tools.append(trigger_automation)

    # -- getAutomationRuns ------------------------------------------------

    @tool(
        "getAutomationRuns",
        "Get run history for an automation.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum runs to return (default 10).",
                },
            },
            "required": ["automation_id"],
        },
    )
    async def get_automation_runs(args: dict) -> dict:
        def _load():
            """Existence check + the run history in ONE hop. The limit is still
            clamped only after the row is known to exist, so an unknown id
            reports "not found" exactly as before."""
            auto = database.get_automation(args["automation_id"])
            if not auto:
                return None, []
            limit = min(args.get("limit", 10), 50)
            return auto, database.get_automation_runs(args["automation_id"], limit=limit)

        auto, runs = await run_sync(_load)
        if not auto:
            return _error(f"Automation not found: {args['automation_id']}")

        return _tool_result({
            "automation_id": args["automation_id"],
            "automation_name": auto.get("name"),
            "runs": [
                {
                    "id": r.get("id"),
                    "status": r.get("status"),
                    "cost_usd": r.get("cost_usd", 0),
                    "tokens_in": r.get("tokens_in", 0),
                    "tokens_out": r.get("tokens_out", 0),
                    "duration_ms": r.get("duration_ms", 0),
                    "started_at": r.get("started_at"),
                    "completed_at": r.get("completed_at"),
                    "error": r.get("error"),
                }
                for r in runs
            ],
            "count": len(runs),
        })

    tools.append(get_automation_runs)

    return tools
