"""
Automations management API routes.

Provides CRUD for automations, lifecycle actions (enable/disable/trigger/test),
run history, approval workflow, and statistics.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Depends, Request, status

from app.core.models import (
    Automation, AutomationCreate, AutomationUpdate,
    AutomationRun, AutomationApproval, AutomationStats,
    AutomationTestResult, VALID_AUTOMATION_TRIGGER_TYPES,
)
from app.api.auth import require_admin
from app.db import database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/automations", tags=["Automations"])


# ---- CRUD ----

@router.get("", response_model=List[Automation])
async def list_automations(token: str = Depends(require_admin)):
    """List all automations."""
    return database.get_all_automations()


@router.post("", response_model=Automation, status_code=status.HTTP_201_CREATED)
async def create_automation_endpoint(
    data: AutomationCreate,
    token: str = Depends(require_admin),
):
    """Create a new automation."""
    from app.core.automation_service import create_automation
    try:
        return await create_automation(
            name=data.name,
            trigger_type=data.trigger_type.value,
            trigger_config=data.trigger_config,
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
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/trigger-types")
async def list_trigger_types(token: str = Depends(require_admin)):
    """List available trigger types."""
    return VALID_AUTOMATION_TRIGGER_TYPES


@router.get("/approvals/pending", response_model=List[AutomationApproval])
async def list_pending_approvals(token: str = Depends(require_admin)):
    """List all pending automation approvals."""
    return database.get_pending_automation_approvals()


@router.post("/approvals/{approval_id}/approve", response_model=AutomationApproval)
async def approve_run_endpoint(approval_id: str, token: str = Depends(require_admin)):
    """Approve a pending automation run."""
    approval = database.get_automation_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
    if approval["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Approval is already {approval['status']}")

    database.update_automation_approval(approval_id, status="approved", decided_by="admin")
    database.update_automation_run(approval["run_id"], status="approved")

    # Resume execution
    from app.core.automation_manager import automation_manager
    await automation_manager.execute_approved_run(approval_id)

    return database.get_automation_approval(approval_id)


@router.post("/approvals/{approval_id}/deny", response_model=AutomationApproval)
async def deny_run_endpoint(approval_id: str, token: str = Depends(require_admin)):
    """Deny a pending automation run."""
    approval = database.get_automation_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found")
    if approval["status"] != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Approval is already {approval['status']}")

    database.update_automation_approval(approval_id, status="denied", decided_by="admin")
    database.update_automation_run(approval["run_id"], status="cancelled")

    return database.get_automation_approval(approval_id)


@router.get("/{automation_id}", response_model=Automation)
async def get_automation(automation_id: str, token: str = Depends(require_admin)):
    """Get an automation by ID."""
    automation = database.get_automation(automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
    return automation


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
    try:
        result = await update_automation(automation_id, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
    return result


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
    automation = database.get_automation(automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")

    database.update_automation(automation_id, is_enabled=True)

    from app.core.automation_manager import automation_manager
    trigger_type = automation.get("trigger_type")
    if trigger_type in ("cron", "interval"):
        await automation_manager.schedule_automation(automation_id)

    return database.get_automation(automation_id)


@router.post("/{automation_id}/disable", response_model=Automation)
async def disable_automation(automation_id: str, token: str = Depends(require_admin)):
    """Disable an automation and unschedule it."""
    automation = database.get_automation(automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")

    from app.core.automation_manager import automation_manager
    await automation_manager.unschedule_automation(automation_id)
    database.update_automation(automation_id, is_enabled=False, status="idle")

    return database.get_automation(automation_id)


@router.post("/{automation_id}/trigger")
async def trigger_automation(
    automation_id: str,
    request: Request,
    token: str = Depends(require_admin),
):
    """Manually trigger an automation, or receive a webhook trigger.

    For webhook triggers, include the ``X-Webhook-Signature`` header.
    """
    automation = database.get_automation(automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")

    from app.core.automation_manager import automation_manager

    if automation.get("trigger_type") == "webhook":
        # Webhook trigger
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        signature = request.headers.get("X-Webhook-Signature")
        result = await automation_manager.trigger_webhook(automation_id, payload, signature)
        if not result:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook validation failed")
        return result
    else:
        # Manual trigger
        result = await automation_manager.trigger_manual(automation_id)
        if not result:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Failed to trigger automation")
        return result


@router.post("/{automation_id}/test", response_model=AutomationTestResult)
async def test_automation_endpoint(automation_id: str, token: str = Depends(require_admin)):
    """Dry-run: render prompt and check budget without executing."""
    from app.core.automation_service import test_automation
    automation = database.get_automation(automation_id)
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
    automation = database.get_automation(automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
    return database.get_automation_runs(automation_id, limit=limit, offset=offset)


@router.get("/{automation_id}/runs/{run_id}", response_model=AutomationRun)
async def get_automation_run(automation_id: str, run_id: str, token: str = Depends(require_admin)):
    """Get a specific automation run."""
    run = database.get_automation_run(run_id)
    if not run or run.get("automation_id") != automation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.get("/{automation_id}/stats", response_model=AutomationStats)
async def get_automation_stats_endpoint(automation_id: str, token: str = Depends(require_admin)):
    """Get statistics for an automation."""
    from app.core.automation_service import get_automation_stats
    automation = database.get_automation(automation_id)
    if not automation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Automation not found: {automation_id}")
    return get_automation_stats(automation_id)
