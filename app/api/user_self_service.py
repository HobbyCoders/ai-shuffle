"""
User Self-Service API routes - for API users to manage their own credentials and settings
"""

import logging
import httpx
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.db import database
from app.api.auth import get_current_api_user
from app.core import encryption

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/me", tags=["User Self-Service"])


# ============================================================================
# Request/Response Models
# ============================================================================

class UserProfileResponse(BaseModel):
    """Current user's profile"""
    id: str
    name: str
    username: Optional[str]
    description: Optional[str]
    project_id: Optional[str]
    profile_id: Optional[str]
    is_active: bool
    web_login_allowed: bool
    created_at: str
    updated_at: str
    last_used_at: Optional[str]


class UpdateProfileRequest(BaseModel):
    """Request to update profile"""
    name: Optional[str] = None
    description: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    """Request to change password"""
    current_password: str
    new_password: str


class CredentialStatusResponse(BaseModel):
    """Status of a credential for the user"""
    credential_type: str
    name: str
    description: str
    policy: str  # 'admin_provided', 'user_provided', 'optional'
    is_set: bool  # Whether user has set their own
    admin_available: bool  # Whether admin has set a fallback
    masked_value: Optional[str] = None  # Masked version if set


class AllCredentialsResponse(BaseModel):
    """All credentials status for the user"""
    credentials: list[CredentialStatusResponse]
    has_missing_required: bool  # True if any required credentials are missing


class SetCredentialRequest(BaseModel):
    """Request to set a credential"""
    value: str


class GitHubConfigResponse(BaseModel):
    """User's GitHub configuration"""
    connected: bool
    github_username: Optional[str] = None
    github_avatar_url: Optional[str] = None
    default_repo: Optional[str] = None
    default_branch: Optional[str] = None


class ConnectGitHubRequest(BaseModel):
    """Request to connect GitHub with PAT"""
    personal_access_token: str


class SetGitHubDefaultsRequest(BaseModel):
    """Request to set default repo/branch"""
    default_repo: Optional[str] = None
    default_branch: Optional[str] = None


class GitHubRepoResponse(BaseModel):
    """GitHub repository info"""
    full_name: str
    name: str
    owner: str
    private: bool
    description: Optional[str]
    default_branch: str


class GitHubBranchResponse(BaseModel):
    """GitHub branch info"""
    name: str
    is_default: bool


# ============================================================================
# Helpers
# ============================================================================

def mask_api_key(key: str) -> str:
    """Mask an API key for display"""
    if not key or len(key) < 10:
        return "****"
    return f"{key[:7]}...{key[-4:]}"


def get_decrypted_user_credential(api_user_id: str, credential_type: str) -> Optional[str]:
    """Get a user's credential, decrypted"""
    cred = database.get_user_credential(api_user_id, credential_type)
    if not cred:
        return None

    encrypted_value = cred.get("encrypted_value")
    if not encrypted_value:
        return None

    if encryption.is_encrypted(encrypted_value):
        if not encryption.is_encryption_ready():
            logger.warning(f"Cannot decrypt user credential - encryption key not loaded")
            return None
        try:
            return encryption.decrypt_value(encrypted_value)
        except Exception as e:
            logger.error(f"Failed to decrypt user credential: {e}")
            return None
    else:
        return encrypted_value


def get_admin_api_key(setting_name: str) -> Optional[str]:
    """Get admin's API key (decrypted)"""
    value = database.get_system_setting(setting_name)
    if not value:
        return None

    if encryption.is_encrypted(value):
        if not encryption.is_encryption_ready():
            return None
        try:
            return encryption.decrypt_value(value)
        except Exception:
            return None
    return value


CREDENTIAL_INFO = {
    "openai_api_key": {
        "name": "OpenAI API Key",
        "description": "For TTS, STT, GPT Image, and Sora video generation",
        "admin_setting": "openai_api_key"
    },
    "gemini_api_key": {
        "name": "Google Gemini API Key",
        "description": "For Nano Banana image generation, Imagen, and Veo video",
        "admin_setting": "image_api_key"
    },
    "github_pat": {
        "name": "GitHub Personal Access Token",
        "description": "For accessing your GitHub repositories",
        "admin_setting": None  # No admin fallback for GitHub
    }
}


# ============================================================================
# Profile Endpoints
# ============================================================================

@router.get("", response_model=UserProfileResponse)
async def get_my_profile(api_user: dict = Depends(get_current_api_user)):
    """Get current user's profile"""
    return UserProfileResponse(
        id=api_user["id"],
        name=api_user["name"],
        username=api_user.get("username"),
        description=api_user.get("description"),
        project_id=api_user.get("project_id"),
        profile_id=api_user.get("profile_id"),
        is_active=api_user["is_active"],
        web_login_allowed=api_user.get("web_login_allowed", True),
        created_at=api_user["created_at"],
        updated_at=api_user["updated_at"],
        last_used_at=api_user.get("last_used_at")
    )


@router.put("")
async def update_my_profile(
    request: UpdateProfileRequest,
    api_user: dict = Depends(get_current_api_user)
):
    """Update current user's profile (name, description)"""
    updates = {}
    if request.name is not None:
        if not request.name.strip():
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        updates["name"] = request.name.strip()
    if request.description is not None:
        updates["description"] = request.description.strip() if request.description else None

    if updates:
        updated = database.update_api_user(api_user["id"], **updates)
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to update profile")
        return updated

    return api_user


@router.put("/password")
async def change_my_password(
    request: ChangePasswordRequest,
    api_user: dict = Depends(get_current_api_user)
):
    """Change current user's password"""
    import bcrypt

    # Verify current password
    if not api_user.get("password_hash"):
        raise HTTPException(status_code=400, detail="Account does not have a password set")

    if not bcrypt.checkpw(request.current_password.encode(), api_user["password_hash"].encode()):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    # Validate new password
    if len(request.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    if request.new_password == request.current_password:
        raise HTTPException(status_code=400, detail="New password must be different from current")

    # Hash and save new password
    new_hash = bcrypt.hashpw(request.new_password.encode(), bcrypt.gensalt()).decode()
    if not database.update_api_user_password(api_user["id"], new_hash):
        raise HTTPException(status_code=500, detail="Failed to update password")

    return {"success": True, "message": "Password changed successfully"}


# ============================================================================
# Credentials Endpoints
# ============================================================================

@router.get("/credentials", response_model=AllCredentialsResponse)
async def get_my_credentials(api_user: dict = Depends(get_current_api_user)):
    """
    Get status of all credentials for the current user.

    Shows which credentials are required, optional, or admin-provided,
    and whether the user has set their own values.
    """
    policies = {p["id"]: p for p in database.get_all_credential_policies()}
    credentials = []
    has_missing_required = False

    for cred_type, info in CREDENTIAL_INFO.items():
        policy = policies.get(cred_type, {}).get("policy", "optional")

        # Check if user has set their own
        user_cred = get_decrypted_user_credential(api_user["id"], cred_type)
        is_set = bool(user_cred)

        # Check if admin has set a fallback
        admin_setting = info.get("admin_setting")
        admin_available = bool(get_admin_api_key(admin_setting)) if admin_setting else False

        # Mask the value if set
        masked_value = mask_api_key(user_cred) if user_cred else None

        # Check if this is a required credential that's missing
        if policy == "user_provided" and not is_set:
            has_missing_required = True

        credentials.append(CredentialStatusResponse(
            credential_type=cred_type,
            name=info["name"],
            description=info["description"],
            policy=policy,
            is_set=is_set,
            admin_available=admin_available,
            masked_value=masked_value
        ))

    return AllCredentialsResponse(
        credentials=credentials,
        has_missing_required=has_missing_required
    )


@router.get("/credentials/requirements")
async def get_credential_requirements(api_user: dict = Depends(get_current_api_user)):
    """
    Get which credentials the user needs to provide based on policies.

    Returns a simplified view for UI to show required vs optional credentials.
    """
    policies = {p["id"]: p for p in database.get_all_credential_policies()}
    requirements = {
        "required": [],  # User MUST provide these
        "optional": [],  # User CAN provide these (admin has fallback)
        "admin_provided": []  # Admin provides, user cannot set
    }

    for cred_type, info in CREDENTIAL_INFO.items():
        policy_obj = policies.get(cred_type, {})
        policy = policy_obj.get("policy", "optional")

        cred_info = {
            "credential_type": cred_type,
            "name": info["name"],
            "description": info["description"],
            "is_set": database.user_has_credential(api_user["id"], cred_type)
        }

        if policy == "user_provided":
            requirements["required"].append(cred_info)
        elif policy == "optional":
            # Check if admin has a fallback
            admin_setting = info.get("admin_setting")
            has_admin = bool(get_admin_api_key(admin_setting)) if admin_setting else False
            cred_info["admin_available"] = has_admin
            requirements["optional"].append(cred_info)
        else:  # admin_provided
            requirements["admin_provided"].append(cred_info)

    return requirements


@router.post("/credentials/{credential_type}")
async def set_my_credential(
    credential_type: str,
    request: SetCredentialRequest,
    api_user: dict = Depends(get_current_api_user)
):
    """
    Set a credential for the current user.

    The credential will be encrypted before storage.
    """
    if credential_type not in CREDENTIAL_INFO:
        raise HTTPException(status_code=400, detail=f"Invalid credential type: {credential_type}")

    # Check policy - user cannot set admin_provided credentials
    policy = database.get_credential_policy(credential_type)
    if policy and policy.get("policy") == "admin_provided":
        raise HTTPException(
            status_code=403,
            detail="This credential is managed by the administrator"
        )

    value = request.value.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Credential value cannot be empty")

    # Validate the credential based on type
    if credential_type == "openai_api_key":
        if not value.startswith("sk-"):
            raise HTTPException(status_code=400, detail="Invalid OpenAI API key format")
        # Optionally validate with OpenAI API
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {value}"}
                )
                if response.status_code == 401:
                    raise HTTPException(status_code=400, detail="Invalid OpenAI API key")
        except httpx.TimeoutException:
            pass  # Don't fail on timeout
        except HTTPException:
            raise

    elif credential_type == "gemini_api_key":
        # Validate with Google AI API
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={value}"
                )
                if response.status_code == 400 and "API_KEY_INVALID" in response.text:
                    raise HTTPException(status_code=400, detail="Invalid Google AI API key")
        except httpx.TimeoutException:
            pass
        except HTTPException:
            raise

    elif credential_type == "github_pat":
        # Validate with GitHub API and get user info
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {value}",
                        "Accept": "application/vnd.github+json"
                    }
                )
                if response.status_code == 401:
                    raise HTTPException(status_code=400, detail="Invalid GitHub Personal Access Token")
                elif response.status_code == 200:
                    # Store GitHub user info
                    github_user = response.json()
                    database.set_user_github_config(
                        api_user["id"],
                        github_username=github_user.get("login"),
                        github_avatar_url=github_user.get("avatar_url")
                    )
        except httpx.TimeoutException:
            pass
        except HTTPException:
            raise

    # Encrypt and store
    if encryption.is_encryption_ready():
        encrypted_value = encryption.encrypt_value(value)
    else:
        encrypted_value = value  # Fallback to plaintext if encryption not ready

    database.set_user_credential(api_user["id"], credential_type, encrypted_value)

    return {
        "success": True,
        "credential_type": credential_type,
        "masked_value": mask_api_key(value)
    }


@router.delete("/credentials/{credential_type}")
async def delete_my_credential(
    credential_type: str,
    api_user: dict = Depends(get_current_api_user)
):
    """Remove a credential for the current user"""
    if credential_type not in CREDENTIAL_INFO:
        raise HTTPException(status_code=400, detail=f"Invalid credential type: {credential_type}")

    # Check policy - warn if this is a required credential
    policy = database.get_credential_policy(credential_type)
    is_required = policy and policy.get("policy") == "user_provided"

    deleted = database.delete_user_credential(api_user["id"], credential_type)

    # Also delete GitHub config if removing GitHub PAT
    if credential_type == "github_pat":
        database.delete_user_github_config(api_user["id"])

    return {
        "success": deleted,
        "credential_type": credential_type,
        "warning": "This credential is required for full functionality" if is_required else None
    }


# ============================================================================
# GitHub Endpoints
# ============================================================================

@router.get("/github", response_model=GitHubConfigResponse)
async def get_my_github_config(api_user: dict = Depends(get_current_api_user)):
    """Get current user's GitHub configuration"""
    config = database.get_user_github_config(api_user["id"])
    has_pat = database.user_has_credential(api_user["id"], "github_pat")

    if not config and not has_pat:
        return GitHubConfigResponse(connected=False)

    return GitHubConfigResponse(
        connected=has_pat,
        github_username=config.get("github_username") if config else None,
        github_avatar_url=config.get("github_avatar_url") if config else None,
        default_repo=config.get("default_repo") if config else None,
        default_branch=config.get("default_branch") if config else None
    )


@router.post("/github/connect")
async def connect_github(
    request: ConnectGitHubRequest,
    api_user: dict = Depends(get_current_api_user)
):
    """Connect GitHub by providing a Personal Access Token"""
    # This is just a convenience wrapper around set_my_credential
    pat = request.personal_access_token.strip()
    if not pat:
        raise HTTPException(status_code=400, detail="Personal Access Token cannot be empty")

    # Validate PAT with GitHub API
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json"
                }
            )
            if response.status_code == 401:
                raise HTTPException(status_code=400, detail="Invalid GitHub Personal Access Token")
            elif response.status_code != 200:
                raise HTTPException(status_code=400, detail="Failed to validate GitHub token")

            github_user = response.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to GitHub: {str(e)}")

    # Encrypt and store PAT
    if encryption.is_encryption_ready():
        encrypted_value = encryption.encrypt_value(pat)
    else:
        encrypted_value = pat

    database.set_user_credential(api_user["id"], "github_pat", encrypted_value)

    # Store GitHub user info
    database.set_user_github_config(
        api_user["id"],
        github_username=github_user.get("login"),
        github_avatar_url=github_user.get("avatar_url")
    )

    return {
        "success": True,
        "github_username": github_user.get("login"),
        "github_avatar_url": github_user.get("avatar_url")
    }


@router.delete("/github")
async def disconnect_github(api_user: dict = Depends(get_current_api_user)):
    """Disconnect GitHub (remove PAT and config)"""
    database.delete_user_credential(api_user["id"], "github_pat")
    database.delete_user_github_config(api_user["id"])
    return {"success": True}


@router.get("/github/repos")
async def list_my_github_repos(
    page: int = 1,
    per_page: int = 30,
    api_user: dict = Depends(get_current_api_user)
):
    """List GitHub repositories accessible to the user"""
    pat = get_decrypted_user_credential(api_user["id"], "github_pat")
    if not pat:
        raise HTTPException(status_code=400, detail="GitHub not connected. Please connect GitHub first.")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://api.github.com/user/repos",
                params={
                    "page": page,
                    "per_page": per_page,
                    "sort": "updated",
                    "direction": "desc"
                },
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json"
                }
            )
            if response.status_code == 401:
                raise HTTPException(status_code=401, detail="GitHub token expired or invalid")
            elif response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Failed to list repositories")

            repos = response.json()
            return {
                "repos": [
                    GitHubRepoResponse(
                        full_name=repo["full_name"],
                        name=repo["name"],
                        owner=repo["owner"]["login"],
                        private=repo["private"],
                        description=repo.get("description"),
                        default_branch=repo.get("default_branch", "main")
                    ).model_dump()
                    for repo in repos
                ],
                "page": page,
                "per_page": per_page
            }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list repos: {str(e)}")


@router.get("/github/branches/{owner}/{repo}")
async def list_github_branches(
    owner: str,
    repo: str,
    api_user: dict = Depends(get_current_api_user)
):
    """List branches for a GitHub repository"""
    pat = get_decrypted_user_credential(api_user["id"], "github_pat")
    if not pat:
        raise HTTPException(status_code=400, detail="GitHub not connected")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get branches
            response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/branches",
                params={"per_page": 100},
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json"
                }
            )
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="Repository not found or no access")
            elif response.status_code == 401:
                raise HTTPException(status_code=401, detail="GitHub token expired or invalid")
            elif response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Failed to list branches")

            branches = response.json()

            # Get default branch
            repo_response = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json"
                }
            )
            default_branch = "main"
            if repo_response.status_code == 200:
                default_branch = repo_response.json().get("default_branch", "main")

            return {
                "branches": [
                    GitHubBranchResponse(
                        name=branch["name"],
                        is_default=branch["name"] == default_branch
                    ).model_dump()
                    for branch in branches
                ],
                "default_branch": default_branch
            }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GitHub API timeout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list branches: {str(e)}")


@router.post("/github/config")
async def set_github_defaults(
    request: SetGitHubDefaultsRequest,
    api_user: dict = Depends(get_current_api_user)
):
    """Set default repository and branch for GitHub operations"""
    if not database.user_has_credential(api_user["id"], "github_pat"):
        raise HTTPException(status_code=400, detail="GitHub not connected")

    config = database.set_user_github_config(
        api_user["id"],
        default_repo=request.default_repo,
        default_branch=request.default_branch
    )

    return {
        "success": True,
        "default_repo": config.get("default_repo"),
        "default_branch": config.get("default_branch")
    }
