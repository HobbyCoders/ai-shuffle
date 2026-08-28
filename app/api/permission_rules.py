"""
Permission Rules API routes

Manages persistent permission rules for tool access control.
Rules can be profile-level (persist across sessions) or session-level (in-memory only).
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_admin

router = APIRouter(prefix="/api/v1/permission-rules", tags=["Permission Rules"])


class PermissionRuleResponse(BaseModel):
    """Permission rule response model"""
    id: str
    profile_id: Optional[str] = None
    tool_name: str
    tool_pattern: Optional[str] = None
    decision: str  # 'allow' or 'deny'
    created_at: str


class PermissionRuleCreate(BaseModel):
    """Create a permission rule"""
    profile_id: str = Field(..., description="Profile ID to apply the rule to")
    tool_name: str = Field(..., description="Tool name (e.g., 'Bash', 'Read', '*')")
    tool_pattern: Optional[str] = Field(None, description="Optional pattern for matching (e.g., 'npm *', '/workspace/*')")
    decision: str = Field(..., description="'allow' or 'deny'")


@router.get("", response_model=List[PermissionRuleResponse])
async def list_permission_rules(
    profile_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    token: str = Depends(require_admin)
):
    """
    List permission rules with optional filters.
    Admin only - these are profile-level rules.
    """
    rules = await run_sync(
        database.get_permission_rules, profile_id=profile_id, tool_name=tool_name
    )
    return rules


@router.get("/profile/{profile_id}", response_model=List[PermissionRuleResponse])
async def get_profile_permission_rules(
    profile_id: str,
    token: str = Depends(require_admin)
):
    """Get all permission rules for a specific profile."""
    def _load():
        """Existence probe + rule read in ONE hop.

        The 404 short-circuits before the rule query exactly as before — the
        second read is never reached for an unknown profile.
        """
        profile = database.get_profile(profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile not found: {profile_id}"
            )
        return database.get_permission_rules(profile_id=profile_id)

    rules = await run_sync(_load)
    return rules


@router.post("", response_model=PermissionRuleResponse)
async def create_permission_rule(
    rule: PermissionRuleCreate,
    token: str = Depends(require_admin)
):
    """
    Create a new permission rule for a profile.
    Admin only.
    """
    # Validate decision
    if rule.decision not in ("allow", "deny"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Decision must be 'allow' or 'deny'"
        )

    def _create():
        """Profile check + insert in ONE hop.

        Read-decide-write: run as two hops the profile could be deleted between
        the check and the insert, so the rule would be written against a profile
        that no longer exists (a race that does not exist today).
        """
        # Verify profile exists
        profile = database.get_profile(rule.profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile not found: {rule.profile_id}"
            )

        # Create the rule
        return database.add_permission_rule(
            profile_id=rule.profile_id,
            tool_name=rule.tool_name,
            tool_pattern=rule.tool_pattern,
            decision=rule.decision
        )

    new_rule = await run_sync(_create)

    return new_rule


@router.delete("/{rule_id}")
async def delete_permission_rule(
    rule_id: str,
    token: str = Depends(require_admin)
):
    """Delete a permission rule. Admin only."""
    def _delete():
        """Existence probe + delete in ONE hop.

        Read-decide-write: split across two hops a concurrent delete between
        them would turn the 404 into a spurious 500 ("Failed to delete rule").
        Both HTTPExceptions propagate out of ``run_sync`` unchanged.
        """
        rule = database.get_permission_rule(rule_id)
        if not rule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rule not found: {rule_id}"
            )

        success = database.delete_permission_rule(rule_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete rule"
            )

    await run_sync(_delete)

    return {"deleted": True, "id": rule_id}


@router.delete("/profile/{profile_id}")
async def delete_profile_permission_rules(
    profile_id: str,
    token: str = Depends(require_admin)
):
    """Delete all permission rules for a profile. Admin only."""
    def _delete_all():
        """Profile check + bulk delete in ONE hop (read-decide-write)."""
        # Verify profile exists
        profile = database.get_profile(profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile not found: {profile_id}"
            )

        return database.delete_profile_permission_rules(profile_id)

    count = await run_sync(_delete_all)
    return {"deleted": count, "profile_id": profile_id}
