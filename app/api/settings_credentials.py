"""
Settings API routes - credential policy endpoints.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import database
from app.api.auth import require_admin, require_auth
from app.api.settings_crypto import get_decrypted_api_key

logger = logging.getLogger(__name__)

credentials_router = APIRouter(tags=["Settings"])

# ============================================================================
# Pydantic Models
# ============================================================================


class CredentialPolicyResponse(BaseModel):
    """A credential policy"""
    id: str
    policy: str  # 'admin_provided', 'user_provided', 'optional'
    description: Optional[str]
    updated_at: str


class UpdateCredentialPolicyRequest(BaseModel):
    """Request to update a credential policy"""
    policy: str  # 'admin_provided', 'user_provided', 'optional'


class AllCredentialPoliciesResponse(BaseModel):
    """All credential policies"""
    policies: list[CredentialPolicyResponse]


class UserCredentialPolicyResponse(BaseModel):
    """A user's credential policy override"""
    api_user_id: str
    credential_type: str
    policy: str  # 'admin_provided', 'user_provided', 'optional'
    source: str  # 'user' (override) or 'global' (fallback)


class SetUserCredentialPolicyRequest(BaseModel):
    """Request to set a user's credential policy override"""
    policy: str  # 'admin_provided', 'user_provided', 'optional'


# ============================================================================
# Credential Policy Info
# ============================================================================

CREDENTIAL_POLICY_INFO = {
    "fal_api_key": {
        "name": "fal.ai API Key",
        "description": "For all AI tools (image, video, 3D, TTS, STT)",
        "admin_has_key": lambda: bool(get_decrypted_api_key("fal_api_key"))
    },
    "github_pat": {
        "name": "GitHub Personal Access Token",
        "description": "For accessing GitHub repositories",
        "admin_has_key": lambda: False
    }
}


# ============================================================================
# Helper
# ============================================================================


def _compute_effective_status(policy: str, admin_has_key: bool) -> str:
    """Compute the effective status for a credential policy"""
    if policy == "admin_provided":
        return "admin_provides" if admin_has_key else "needs_admin_key"
    elif policy == "user_provided":
        return "user_must_provide"
    else:  # optional
        return "optional_with_fallback" if admin_has_key else "optional_no_fallback"


# ============================================================================
# Credential Policy Endpoints
# ============================================================================


@credentials_router.get("/credential-policies", response_model=AllCredentialPoliciesResponse)
async def get_credential_policies(token: str = Depends(require_auth)):
    """
    Get all credential policies.

    Returns policies that control whether users must provide their own API keys
    or can use admin-provided keys.
    """
    policies = database.get_all_credential_policies()
    enriched = []
    for policy in policies:
        info = CREDENTIAL_POLICY_INFO.get(policy["id"], {})
        enriched.append({
            **policy,
            "name": info.get("name", policy["id"]),
            "admin_has_key": info.get("admin_has_key", lambda: False)()
        })
    return AllCredentialPoliciesResponse(
        policies=[CredentialPolicyResponse(**p) for p in policies]
    )


@credentials_router.put("/credential-policies/{policy_id}")
async def update_credential_policy(
    policy_id: str,
    request: UpdateCredentialPolicyRequest,
    token: str = Depends(require_admin)
):
    """
    Update a credential policy (admin only).

    Policy options:
    - 'admin_provided': All users use admin's key, users cannot set their own
    - 'user_provided': Each user must provide their own key
    - 'optional': Users can optionally provide their own key, falls back to admin's
    """
    valid_policies = ['admin_provided', 'user_provided', 'optional']
    if request.policy not in valid_policies:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid policy. Must be one of: {', '.join(valid_policies)}"
        )

    # Special case: github_pat cannot be admin_provided (doesn't make sense)
    if policy_id == "github_pat" and request.policy == "admin_provided":
        raise HTTPException(
            status_code=400,
            detail="GitHub PAT cannot be admin-provided. Each user must use their own GitHub account."
        )

    # Special case: can't set to admin_provided if admin doesn't have the key
    if request.policy == "admin_provided" and policy_id in CREDENTIAL_POLICY_INFO:
        info = CREDENTIAL_POLICY_INFO[policy_id]
        if not info.get("admin_has_key", lambda: False)():
            raise HTTPException(
                status_code=400,
                detail="Cannot set to 'admin_provided' - admin has not configured this key yet"
            )

    updated = database.update_credential_policy(policy_id, request.policy)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Policy '{policy_id}' not found")

    return {
        "success": True,
        "id": policy_id,
        "policy": request.policy
    }


@credentials_router.get("/credential-policies/summary")
async def get_credential_policies_summary(token: str = Depends(require_admin)):
    """
    Get a summary of credential policies with admin key status.

    Used by admin UI to show which keys are configured and policy status.
    """
    policies = database.get_all_credential_policies()
    summary = []

    for policy in policies:
        info = CREDENTIAL_POLICY_INFO.get(policy["id"], {})
        admin_has_key = info.get("admin_has_key", lambda: False)()

        summary.append({
            "id": policy["id"],
            "name": info.get("name", policy["id"]),
            "description": info.get("description", policy.get("description", "")),
            "policy": policy["policy"],
            "admin_has_key": admin_has_key,
            # Compute effective status
            "effective_status": _compute_effective_status(policy["policy"], admin_has_key),
            "updated_at": policy["updated_at"]
        })

    return {"policies": summary}


# ============================================================================
# Per-User Credential Policy Overrides
# ============================================================================


@credentials_router.get("/users/{user_id}/credential-policies")
async def get_user_credential_policies(
    user_id: str,
    token: str = Depends(require_admin)
):
    """
    Get all credential policies for a specific user (admin only).

    Returns the policy for each credential type. Policies are per-user only.
    """
    # Verify user exists
    user = database.get_api_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get user's policies
    user_policies = {
        p["credential_type"]: p["policy"]
        for p in database.get_all_user_credential_policies(user_id)
    }

    # Build result from known credential types
    result = []
    for credential_type, info in CREDENTIAL_POLICY_INFO.items():
        policy = user_policies.get(credential_type, "user_provided")  # Default to user_provided

        result.append({
            "credential_type": credential_type,
            "name": info.get("name", credential_type),
            "description": info.get("description", ""),
            "policy": policy,
            "source": "user" if credential_type in user_policies else "default"
        })

    return {
        "user_id": user_id,
        "user_name": user.get("name", "Unknown"),
        "policies": result
    }


@credentials_router.put("/users/{user_id}/credential-policies/{credential_type}")
async def set_user_credential_policy(
    user_id: str,
    credential_type: str,
    request: SetUserCredentialPolicyRequest,
    token: str = Depends(require_admin)
):
    """
    Set the credential policy for a specific user (admin only).

    Policies are per-user only - this sets the user's policy for this credential type.
    """
    # Verify user exists
    user = database.get_api_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Validate policy
    valid_policies = ['admin_provided', 'user_provided', 'optional']
    if request.policy not in valid_policies:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid policy. Must be one of: {', '.join(valid_policies)}"
        )

    # Validate credential type
    if credential_type not in CREDENTIAL_POLICY_INFO:
        raise HTTPException(status_code=400, detail=f"Unknown credential type: {credential_type}")

    # Special case: github_pat cannot be admin_provided
    if credential_type == "github_pat" and request.policy == "admin_provided":
        raise HTTPException(
            status_code=400,
            detail="GitHub PAT cannot be admin-provided. Each user must use their own GitHub account."
        )

    # Special case: can't set to admin_provided if admin doesn't have the key
    if request.policy == "admin_provided":
        info = CREDENTIAL_POLICY_INFO[credential_type]
        if not info.get("admin_has_key", lambda: False)():
            raise HTTPException(
                status_code=400,
                detail="Cannot set to 'admin_provided' - admin has not configured this key yet"
            )

    # Set the override
    updated = database.set_user_credential_policy(user_id, credential_type, request.policy)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to set policy override")

    return {
        "success": True,
        "user_id": user_id,
        "credential_type": credential_type,
        "policy": request.policy,
        "source": "user"
    }


@credentials_router.delete("/users/{user_id}/credential-policies/{credential_type}")
async def delete_user_credential_policy(
    user_id: str,
    credential_type: str,
    token: str = Depends(require_admin)
):
    """
    Reset a credential policy to default for a user (admin only).

    The user will revert to the default policy (user_provided).
    """
    # Verify user exists
    user = database.get_api_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    deleted = database.delete_user_credential_policy(user_id, credential_type)

    # Default policy when none is set
    fallback_policy = "user_provided"

    return {
        "success": True,
        "user_id": user_id,
        "credential_type": credential_type,
        "deleted": deleted,
        "fallback_policy": fallback_policy,
        "source": "default"
    }


# ============================================================================
# Credential Resolution (for internal use by AI tools)
# ============================================================================


@credentials_router.get("/internal/resolve-credential/{credential_type}")
async def resolve_credential(
    credential_type: str,
    user_id: Optional[str] = None,
    _: str = Depends(require_admin)
):
    """
    Internal endpoint to resolve which API key to use.

    Resolution order based on per-user policy:
    1. If policy is 'user_provided' or 'optional': check user's credential first
    2. If not found and policy is 'admin_provided' or 'optional': use admin's key
    3. If not found: return error

    Policies are per-user only. Default policy is 'user_provided'.

    This endpoint is for internal use by AI tools only.
    Requires admin authentication as it returns decrypted API keys.
    """
    from app.core import encryption as enc

    # Map credential types to admin settings
    admin_setting_map = {
        "fal_api_key": "fal_api_key",
        "github_pat": None  # No admin fallback
    }

    if credential_type not in admin_setting_map:
        raise HTTPException(status_code=400, detail=f"Unknown credential type: {credential_type}")

    # Get per-user policy (no global fallback, default to user_provided)
    if user_id:
        effective = database.get_effective_credential_policy(user_id, credential_type)
        policy = effective.get("policy", "user_provided")
    else:
        # No user specified, default to user_provided (won't resolve any key)
        policy = "user_provided"

    resolved_key = None
    source = None

    # Try user's credential if applicable
    if user_id and policy in ["user_provided", "optional"]:
        user_cred = database.get_user_credential(user_id, credential_type)
        if user_cred:
            encrypted = user_cred.get("encrypted_value")
            if encrypted:
                if enc.is_encrypted(encrypted) and enc.is_encryption_ready():
                    try:
                        resolved_key = enc.decrypt_value(encrypted)
                        source = "user"
                    except Exception:
                        pass
                elif not enc.is_encrypted(encrypted):
                    resolved_key = encrypted
                    source = "user"

    # Try admin's key if applicable
    if not resolved_key and policy in ["admin_provided", "optional"]:
        admin_setting = admin_setting_map.get(credential_type)
        if admin_setting:
            resolved_key = get_decrypted_api_key(admin_setting)
            if resolved_key:
                source = "admin"

    if not resolved_key:
        if policy == "user_provided":
            raise HTTPException(
                status_code=404,
                detail=f"Credential '{credential_type}' not configured. User must provide this key."
            )
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Credential '{credential_type}' not configured."
            )

    return {
        "key": resolved_key,
        "source": source,
        "credential_type": credential_type
    }
