"""
Hook management API routes — project-scoped and app-level hooks
"""

import uuid
from typing import List

from fastapi import APIRouter, HTTPException, Depends, Request, status

from app.core.models import (
    Hook,
    HookCreate,
    HookUpdate,
    HookTestResult,
    PluginHookInfo,
    VALID_HOOK_EVENTS,
)
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_admin
from app.api.common import check_project_access

router = APIRouter(prefix="/api/v1/hooks", tags=["Hooks"])


def _validate_hook_data(hook_data: HookCreate | HookUpdate, is_create: bool = False) -> None:
    """Validate hook data"""
    event = getattr(hook_data, 'event', None)
    if event is not None and event not in VALID_HOOK_EVENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid event type: {event}. Valid types are: {', '.join(VALID_HOOK_EVENTS)}"
        )

    hook_type = getattr(hook_data, 'hook_type', None)
    if is_create:
        # For create, validate that required fields are provided based on type
        if hook_type == 'bash' and not hook_data.command:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Bash hooks must have a command"
            )
        if hook_type == 'webhook' and not hook_data.url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Webhook hooks must have a URL"
            )


@router.get("/events", response_model=List[str])
async def list_event_types(token: str = Depends(require_admin)):
    """List all valid hook event types"""
    return VALID_HOOK_EVENTS


# =========================================================================
# App-level hooks (no project)
# =========================================================================

@router.get("/app", response_model=List[Hook])
async def get_app_hooks(
    request: Request,
    token: str = Depends(require_admin)
):
    """Get all app-level hooks (not scoped to any project)"""
    return await run_sync(database.get_app_hooks)


@router.post("/app", response_model=Hook, status_code=status.HTTP_201_CREATED)
async def create_app_hook(
    hook_data: HookCreate,
    request: Request,
    token: str = Depends(require_admin)
):
    """Create a new app-level hook (fires for all sessions)"""
    _validate_hook_data(hook_data, is_create=True)

    hook_id = f"hook-{uuid.uuid4().hex[:12]}"
    # A single isolated write — no read to keep it atomic with.
    hook = await run_sync(
        database.create_hook,
        hook_id=hook_id,
        project_id=None,
        name=hook_data.name,
        event=hook_data.event,
        hook_type=hook_data.hook_type,
        command=hook_data.command,
        timeout=hook_data.timeout,
        url=hook_data.url,
        secret=hook_data.secret,
        enabled=hook_data.enabled,
        tool_matcher=hook_data.tool_matcher,
    )
    return hook


# =========================================================================
# Project-scoped hooks
# =========================================================================

@router.get("/project/{project_id}", response_model=List[Hook])
async def get_project_hooks(
    project_id: str,
    request: Request,
    token: str = Depends(require_admin)
):
    """Get all hooks for a project (project-scoped + app-level)"""
    check_project_access(request, project_id)

    def _load():
        # Verify project exists
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

        return database.get_all_hooks_for_project(project_id)

    return await run_sync(_load)


@router.post("/project/{project_id}", response_model=Hook, status_code=status.HTTP_201_CREATED)
async def create_project_hook(
    project_id: str,
    hook_data: HookCreate,
    request: Request,
    token: str = Depends(require_admin)
):
    """Create a new hook for a project"""
    check_project_access(request, project_id)

    def _create():
        """Existence check -> validate -> insert, in ONE hop.

        read-decide-write: split across hops, a project deleted in between
        would leave the insert pointing at a vanished project.
        """
        # Verify project exists
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Project not found: {project_id}"
            )

        _validate_hook_data(hook_data, is_create=True)

        hook_id = f"hook-{uuid.uuid4().hex[:12]}"
        return database.create_hook(
            hook_id=hook_id,
            project_id=project_id,
            name=hook_data.name,
            event=hook_data.event,
            hook_type=hook_data.hook_type,
            command=hook_data.command,
            timeout=hook_data.timeout,
            url=hook_data.url,
            secret=hook_data.secret,
            enabled=hook_data.enabled,
            tool_matcher=hook_data.tool_matcher,
        )

    return await run_sync(_create)


# =========================================================================
# Individual hook operations
# =========================================================================

@router.get("/{hook_id}", response_model=Hook)
async def get_hook(
    hook_id: str,
    request: Request,
    token: str = Depends(require_admin)
):
    """Get a hook by ID"""
    hook = await run_sync(database.get_hook, hook_id)
    if not hook:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hook not found: {hook_id}"
        )

    # Only check project access for project-scoped hooks
    if hook.get("project_id"):
        check_project_access(request, hook["project_id"])
    return hook


@router.patch("/{hook_id}", response_model=Hook)
async def update_hook(
    hook_id: str,
    hook_data: HookUpdate,
    request: Request,
    token: str = Depends(require_admin)
):
    """Update a hook"""

    def _update():
        """read -> authorise -> validate -> write, in ONE hop.

        The access check reads only ``request.state`` (no I/O), so it stays
        with the row it authorises rather than being split off; splitting the
        read from the write would let a concurrent delete/re-scope land in
        between.
        """
        existing = database.get_hook(hook_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Hook not found: {hook_id}"
            )

        if existing.get("project_id"):
            check_project_access(request, existing["project_id"])
        _validate_hook_data(hook_data)

        return database.update_hook(
            hook_id=hook_id,
            name=hook_data.name,
            event=hook_data.event,
            hook_type=hook_data.hook_type,
            command=hook_data.command,
            timeout=hook_data.timeout,
            url=hook_data.url,
            secret=hook_data.secret,
            enabled=hook_data.enabled,
            tool_matcher=hook_data.tool_matcher,
        )

    return await run_sync(_update)


@router.delete("/{hook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hook(
    hook_id: str,
    request: Request,
    token: str = Depends(require_admin)
):
    """Delete a hook"""

    def _delete():
        """read -> authorise -> delete, in ONE hop (see update_hook)."""
        existing = database.get_hook(hook_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Hook not found: {hook_id}"
            )

        if existing.get("project_id"):
            check_project_access(request, existing["project_id"])

        if not database.delete_hook(hook_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Hook not found: {hook_id}"
            )

    await run_sync(_delete)


@router.post("/{hook_id}/test", response_model=HookTestResult)
async def test_hook(
    hook_id: str,
    request: Request,
    token: str = Depends(require_admin)
):
    """Test a hook with sample event data"""
    hook = await run_sync(database.get_hook, hook_id)
    if not hook:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Hook not found: {hook_id}"
        )

    if hook.get("project_id"):
        check_project_access(request, hook["project_id"])

    # Import hook executor and run test
    from app.core.hook_executor import test_hook as execute_test_hook

    result = await execute_test_hook(hook)
    return result


# =========================================================================
# Plugin hooks (read-only)
# =========================================================================

@router.get("/plugins/all", response_model=List[PluginHookInfo])
async def get_plugin_hooks(
    request: Request,
    token: str = Depends(require_admin)
):
    """Get all hooks contributed by installed plugins (read-only)"""
    # Import plugin service to get hook info
    from app.core.plugin_service import get_all_plugin_hooks

    # Not a DB call — so the scanner does not see it — but blocking all the
    # same: this reads installed.json and settings.json, then globs every
    # plugin's hooks/ directory and parses each hook file. Run on the loop it
    # stalled the worker for the whole scan.
    return await run_sync(get_all_plugin_hooks)
