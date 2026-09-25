"""
Automations management API routes.

Provides CRUD for automations, lifecycle actions (enable/disable/trigger/test),
run history, approval workflow, and statistics.
"""

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Depends, Request, status

from app.core.models import (
    Automation, AutomationCreate, AutomationUpdate,
    AutomationRun, AutomationApproval, AutomationStats,
    AutomationTestResult, VALID_AUTOMATION_TRIGGER_TYPES,
)
from app.api.auth import require_admin
from app.core.automation_service import reveal_webhook_secrets
from app.db import database
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/automations", tags=["Automations"])


async def _with_notices(automation):
    """Attach "use an API key for scheduled runs" when this automation would
    run on the user's Claude subscription (see app.core.loop_guard)."""
    if not isinstance(automation, dict):
        return automation

    def _notice():
        from app.core import loop_guard
        from app.core.profiles import get_profile

        profile_id = automation.get("profile_id")
        return loop_guard.scheduled_run_guidance(get_profile(profile_id) if profile_id else None)

    try:
        notice = await run_sync(_notice)
    except Exception:  # noqa: BLE001 — advice never fails a save
        logger.warning("Could not work out the scheduled-run notice", exc_info=True)
        notice = None
    return {**automation, "notices": [notice] if notice else []}


# ---- CRUD ----

@router.get("", response_model=List[Automation])
async def list_automations(token: str = Depends(require_admin)):
    """List all automations.

    Owner-only, like every route here, so webhook secrets come back in
    plaintext: the edit form is filled from this list and must show a secret
    the owner can sign with. Run records never carry secrets.
    """
    automations = await run_sync(database.get_all_automations)
    return [reveal_webhook_secrets(a) for a in automations]


@router.post("", response_model=Automation, status_code=status.HTTP_201_CREATED)
async def create_automation_endpoint(
    data: AutomationCreate,
    token: str = Depends(require_admin),
):
    """Create a new automation."""
    from app.core.automation_service import create_automation
    # Convert triggers from Pydantic models to dicts
    triggers_list = None
    if data.triggers:
        triggers_list = [{"type": t.type.value, "config": t.config} for t in data.triggers]
    # Determine primary trigger_type/trigger_config
    if triggers_list:
        primary_type = triggers_list[0]["type"]
        primary_config = triggers_list[0]["config"]
    elif data.trigger_type:
        primary_type = data.trigger_type.value
        primary_config = data.trigger_config
        triggers_list = [{"type": primary_type, "config": primary_config}]
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one trigger is required")
    try:
        created = await create_automation(
            name=data.name,
            trigger_type=primary_type,
            trigger_config=primary_config,
            triggers=triggers_list,
            prompt=data.prompt,
            profile_id=data.profile_id,
            project_id=data.project_id,
            description=data.description,
            output_config=data.output_config,
            max_turns=data.max_turns,
            timeout_seconds=data.timeout_seconds,
            max_cost_per_run=data.max_cost_per_run,
            max_cost_per_day=data.max_cost_per_day,
            max_cost_total=data.max_cost_total,
            max_runs_per_day=data.max_runs_per_day,
            requires_approval=data.requires_approval,
            action_steps=data.action_steps,
            enabled=data.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return await _with_notices(reveal_webhook_secrets(created))


@router.get("/trigger-types")
async def list_trigger_types(token: str = Depends(require_admin)):
    """List available trigger types."""
    return VALID_AUTOMATION_TRIGGER_TYPES


@router.get("/approvals/pending", response_model=List[AutomationApproval])
async def list_pending_approvals(token: str = Depends(require_admin)):
    """List all pending automation approvals."""
    return await run_sync(database.get_pending_automation_approvals)


@router.post("/approvals/{approval_id}/approve", response_model=AutomationApproval)
async def approve_run_endpoint(approval_id: str, token: str = Depends(require_admin)):
    """Approve a pending automation run."""

    def _claim_approval():
        """read -> decide -> write, in ONE hop.

        The pending check and the two writes must stay in a single offload:
        split across hops, two concurrent approvals could both read "pending"
        and both resume the same run. HTTPException raised here propagates out
        of the thread unchanged.
        """
        approval = database.get_automation_approval(approval_id)
        if not approval:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
        if approval["status"] != "pending":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Approval is already {approval['status']}")

        database.update_automation_approval(approval_id, status="approved", decided_by="admin")
        database.update_automation_run(approval["run_id"], status="approved")

    await run_sync(_claim_approval)

    # Resume execution — a real await, so the re-read below cannot join the
    # hop above (the run may mutate the approval row).
    from app.core.automation_manager import automation_manager
    await automation_manager.execute_approved_run(approval_id)

    return await run_sync(database.get_automation_approval, approval_id)


@router.post("/approvals/{approval_id}/deny", response_model=AutomationApproval)
async def deny_run_endpoint(approval_id: str, token: str = Depends(require_admin)):
    """Deny a pending automation run."""

    def _deny():
        """read -> decide -> write -> re-read, in ONE hop (no await splits it)."""
        approval = database.get_automation_approval(approval_id)
        if not approval:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
        if approval["status"] != "pending":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Approval is already {approval['status']}")

        database.update_automation_approval(approval_id, status="denied", decided_by="admin")
        database.update_automation_run(approval["run_id"], status="cancelled")

        return database.get_automation_approval(approval_id)

    return await run_sync(_deny)


@router.get("/{automation_id}", response_model=Automation)
async def get_automation(automation_id: str, token: str = Depends(require_admin)):
    """Get an automation by ID."""
    automation = await run_sync(database.get_automation, automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
    return reveal_webhook_secrets(automation)


@router.patch("/{automation_id}", response_model=Automation)
async def update_automation_endpoint(
    automation_id: str,
    data: AutomationUpdate,
    token: str = Depends(require_admin),
):
    """Update an automation."""
    from app.core.automation_service import update_automation
    kwargs = data.model_dump(exclude_unset=True)
    # Convert enum to string value
    if "trigger_type" in kwargs and kwargs["trigger_type"] is not None:
        kwargs["trigger_type"] = kwargs["trigger_type"].value if hasattr(kwargs["trigger_type"], "value") else kwargs["trigger_type"]
    # Convert triggers from Pydantic models to dicts
    if "triggers" in kwargs and kwargs["triggers"] is not None:
        kwargs["triggers"] = [
            {"type": t["type"].value if hasattr(t["type"], "value") else t["type"], "config": t.get("config", {})}
            for t in kwargs["triggers"]
        ]
        # Also update trigger_type/trigger_config from first trigger for backward compat
        if kwargs["triggers"]:
            kwargs["trigger_type"] = kwargs["triggers"][0]["type"]
            kwargs["trigger_config"] = kwargs["triggers"][0]["config"]
    try:
        result = await update_automation(automation_id, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
    return await _with_notices(reveal_webhook_secrets(result))


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation_endpoint(automation_id: str, token: str = Depends(require_admin)):
    """Delete an automation."""
    from app.core.automation_service import delete_automation
    if not await delete_automation(automation_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")


# ---- Lifecycle Actions ----

@router.post("/{automation_id}/enable", response_model=Automation)
async def enable_automation(automation_id: str, token: str = Depends(require_admin)):
    """Enable an automation and schedule it."""

    def _enable():
        """read -> decide -> write in ONE hop, plus the re-read when possible.

        Keeping the existence check and the enable write together stops a
        concurrent delete from turning this into an update of a vanished row.
        """
        automation = database.get_automation(automation_id)
        if not automation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")

        database.update_automation(automation_id, is_enabled=True)

        # Check if any trigger in the array needs scheduling
        triggers = automation.get("triggers") or []
        has_scheduled = any(t.get("type") in ("cron", "interval") for t in triggers)
        # With nothing to schedule there is no await between the write and the
        # response read, so it belongs to this same hop. When the scheduler
        # does run it may stamp the row (next_run_at), so that path has to
        # re-read *after* the await — see below.
        return has_scheduled, None if has_scheduled else database.get_automation(automation_id)

    has_scheduled, automation = await run_sync(_enable)

    from app.core.automation_manager import automation_manager
    if has_scheduled:
        await automation_manager.schedule_automation(automation_id)
        automation = await run_sync(database.get_automation, automation_id)

    return reveal_webhook_secrets(automation)


@router.post("/{automation_id}/disable", response_model=Automation)
async def disable_automation(automation_id: str, token: str = Depends(require_admin)):
    """Disable an automation and unschedule it."""
    automation = await run_sync(database.get_automation, automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")

    from app.core.automation_manager import automation_manager
    # The unschedule is a real await sitting between the check and the write,
    # so this handler needs two hops; the write and its read-back are
    # contiguous and share the second one.
    await automation_manager.unschedule_automation(automation_id)

    def _disable():
        database.update_automation(automation_id, is_enabled=False, status="idle")
        return database.get_automation(automation_id)

    return reveal_webhook_secrets(await run_sync(_disable))


@router.post("/{automation_id}/trigger")
async def trigger_automation(
    automation_id: str,
    request: Request,
    token: str = Depends(require_admin),
):
    """Manually trigger an automation, or receive a webhook trigger.

    For webhook triggers, include the ``X-Webhook-Signature`` header.
    """
    automation = await run_sync(database.get_automation, automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")

    from app.core.automation_manager import automation_manager

    # Check if any trigger is a webhook type
    triggers = automation.get("triggers") or []
    has_webhook = any(t.get("type") == "webhook" for t in triggers)
    signature = request.headers.get("X-Webhook-Signature")

    if has_webhook and signature:
        # Webhook trigger with signature
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        result = await automation_manager.trigger_webhook(automation_id, payload, signature)
        if not result:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook validation failed")
        return result
    else:
        # Manual trigger. _execute's is_enabled gate silently no-ops a
        # disabled automation, so a fake "triggered" here would mislead the
        # caller into thinking a run started. Reject explicitly — the user
        # must enable the automation first.
        if not automation.get("is_enabled"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Automation is disabled — enable it before triggering",
            )
        result = await automation_manager.trigger_manual(automation_id)
        if not result:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to trigger automation")
        return result


@router.post("/{automation_id}/test", response_model=AutomationTestResult)
async def test_automation_endpoint(automation_id: str, token: str = Depends(require_admin)):
    """Dry-run: render prompt and check budget without executing."""
    from app.core.automation_service import test_automation
    automation = await run_sync(database.get_automation, automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
    return await test_automation(automation_id)


# ---- Runs & Stats ----

@router.get("/{automation_id}/runs", response_model=List[AutomationRun])
async def list_automation_runs(
    automation_id: str,
    limit: int = 50,
    offset: int = 0,
    token: str = Depends(require_admin),
):
    """List execution history for an automation."""

    def _load_runs():
        automation = database.get_automation(automation_id)
        if not automation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
        return database.get_automation_runs(automation_id, limit=limit, offset=offset)

    return await run_sync(_load_runs)


@router.get("/{automation_id}/runs/{run_id}", response_model=AutomationRun)
async def get_automation_run(automation_id: str, run_id: str, token: str = Depends(require_admin)):
    """Get a specific automation run."""
    run = await run_sync(database.get_automation_run, run_id)
    if not run or run.get("automation_id") != automation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.get("/{automation_id}/stats", response_model=AutomationStats)
async def get_automation_stats_endpoint(automation_id: str, token: str = Depends(require_admin)):
    """Get statistics for an automation."""
    from app.core.automation_service import get_automation_stats

    def _stats():
        # ``get_automation_stats`` is itself synchronous and DB-backed, so it
        # joins the existence check in the same hop rather than blocking the
        # loop after it.
        automation = database.get_automation(automation_id)
        if not automation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
        return get_automation_stats(automation_id)

    return await run_sync(_stats)
