"""
Permission Rules API routes

Manages persistent permission rules for tool access control.
Rules can be profile-level (persist across sessions) or session-level (in-memory only).
"""

from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from app.db import database
from app.api.auth import require_auth, require_admin

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
    rules = database.get_permission_rules(profile_id=profile_id, tool_name=tool_name)
    return rules


@router.get("/profile/{profile_id}", response_model=List[PermissionRuleResponse])
async def get_profile_permission_rules(
    profile_id: str,
    token: str = Depends(require_admin)
):
    """Get all permission rules for a specific profile."""
    # Verify profile exists
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    rules = database.get_permission_rules(profile_id=profile_id)
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

    # Verify profile exists
    profile = database.get_profile(rule.profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {rule.profile_id}"
        )

    # Create the rule
    new_rule = database.add_permission_rule(
        profile_id=rule.profile_id,
        tool_name=rule.tool_name,
        tool_pattern=rule.tool_pattern,
        decision=rule.decision
    )

    return new_rule


@router.delete("/{rule_id}")
async def delete_permission_rule(
    rule_id: str,
    token: str = Depends(require_admin)
):
    """Delete a permission rule. Admin only."""
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

    return {"deleted": True, "id": rule_id}


@router.delete("/profile/{profile_id}")
async def delete_profile_permission_rules(
    profile_id: str,
    token: str = Depends(require_admin)
):
    """Delete all permission rules for a profile. Admin only."""
    # Verify profile exists
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    count = database.delete_profile_permission_rules(profile_id)
    return {"deleted": count, "profile_id": profile_id}
