"""
Settings API routes - the internal credential resolver.

The credential-policy editors that lived here had no client and were removed;
the per-user policy storage they wrote (``credential_policies`` /
``user_credential_policies``) is still read by the resolver below and by
``app.core.environment`` when a tool needs a key.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_admin
from app.api.settings_crypto import get_decrypted_api_key

logger = logging.getLogger(__name__)

credentials_router = APIRouter(tags=["Settings"])

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

    def _resolve() -> tuple[str | None, str | None, str]:
        """Policy read, user-credential read and admin-key read in ONE hop.

        The policy read decides which of the following reads happen, so the
        whole resolution stays in a single hop. Decryption is unchanged and is
        safe here: ``app.core.encryption`` builds a fresh ``Fernet`` per call
        from a module-level key and holds no per-thread state. Nothing extra is
        logged, and the plaintext never leaves this function's return value.
        """
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

        return resolved_key, source, policy

    resolved_key, source, policy = await run_sync(_resolve)

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
