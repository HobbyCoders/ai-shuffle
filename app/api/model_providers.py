"""API routes for the model-provider registry (non-Anthropic gateways).

Admin-gated CRUD for the ``model_providers`` table: each row is a gateway
(OpenRouter or a custom Anthropic-compatible endpoint) whose env vars get
injected into a spawned Claude Code session (see ``app/core/environment.py``,
Chunk 3). Mounted under ``/api/v1/model-providers``.

The stored ``auth_token`` (an ``enc:``-prefixed Fernet blob) is NEVER returned
in a response body — only ``auth_token_set`` (bool) and ``auth_token_masked``
(via ``mask_api_key``, same convention as ``IntegrationSettingsResponse``).
"""

import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.auth import require_admin
from app.api.settings_crypto import mask_api_key
from app.core import encryption
from app.db import database as db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/model-providers", tags=["Model Providers"])

_VALID_PROVIDER_TYPES = ("openrouter", "custom")


# ============================================================================
# Pydantic models
# ============================================================================

class ModelProviderCreate(BaseModel):
    """Request body to create a model provider."""
    name: str
    provider_type: str
    base_url: str
    auth_token: Optional[str] = None
    models: Optional[List[Dict[str, Any]]] = None
    default_model: Optional[str] = None
    small_fast_model: Optional[str] = None
    extra_env: Optional[Dict[str, str]] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    enabled: bool = True


class ModelProviderUpdate(BaseModel):
    """Request body for a partial update. All fields optional."""
    name: Optional[str] = None
    provider_type: Optional[str] = None
    base_url: Optional[str] = None
    auth_token: Optional[str] = None
    models: Optional[List[Dict[str, Any]]] = None
    default_model: Optional[str] = None
    small_fast_model: Optional[str] = None
    extra_env: Optional[Dict[str, str]] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    enabled: Optional[bool] = None


class ModelProviderResponse(BaseModel):
    """Public (token-free) representation of a model provider row."""
    id: str
    name: str
    provider_type: str
    base_url: str
    auth_token_set: bool
    auth_token_masked: str
    models: List[Dict[str, Any]]
    default_model: Optional[str] = None
    small_fast_model: Optional[str] = None
    extra_env: Dict[str, str]
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    enabled: bool
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ModelProviderListResponse(BaseModel):
    providers: List[ModelProviderResponse]


# ============================================================================
# Validation helpers
# ============================================================================

def _validate_provider_type(provider_type: Optional[str]) -> None:
    if provider_type is not None and provider_type not in _VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"provider_type must be one of {_VALID_PROVIDER_TYPES}",
        )


def _validate_base_url(base_url: Optional[str], *, required: bool) -> None:
    if base_url is not None and not base_url.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="base_url cannot be empty")
    if required and (base_url is None or not base_url.strip()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="base_url is required")


def _validate_models(models: Optional[List[Dict[str, Any]]]) -> None:
    if models is None:
        return
    if not isinstance(models, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="models must be a list of {id, label} objects")
    for entry in models:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("label"), str)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="each model entry must be an object with string 'id' and 'label'",
            )


def _validate_extra_env(extra_env: Optional[Dict[str, str]]) -> None:
    if extra_env is None:
        return
    if not isinstance(extra_env, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="extra_env must be an object of string to string")
    for key, value in extra_env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="extra_env must be an object of string to string",
            )


def _encrypt_token_or_400(token: str) -> str:
    """Encrypt a non-blank auth token, or raise 400 if encryption isn't ready."""
    if not encryption.is_encryption_ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Encryption is locked. Log in again as admin (or set "
                   "ADMIN_PASSWORD) before storing a provider auth token.",
        )
    return encryption.encrypt_value(token)


def _public_provider(row: Dict[str, Any]) -> ModelProviderResponse:
    """Shape a DB row (public columns only, no token) into the response model."""
    return ModelProviderResponse(
        id=row["id"],
        name=row["name"],
        provider_type=row["provider_type"],
        base_url=row["base_url"],
        auth_token_set=bool(row.get("_auth_token_set")),
        auth_token_masked=row.get("_auth_token_masked", ""),
        models=row.get("models_json") or [],
        default_model=row.get("default_model"),
        small_fast_model=row.get("small_fast_model"),
        extra_env=row.get("extra_env_json") or {},
        context_window=row.get("context_window"),
        max_output_tokens=row.get("max_output_tokens"),
        enabled=bool(row.get("enabled")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _apply_token_flags(row: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the raw ``auth_token`` blob off a row and derive the public
    ``_auth_token_set`` / ``_auth_token_masked`` flags in its place. Mutates and
    returns the same row.
    """
    token = row.pop("auth_token", None)
    row["_auth_token_set"] = bool(token)
    row["_auth_token_masked"] = mask_api_key(token) if token else ""
    return row


def _row_with_token_flags(provider_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a provider WITH the token (server-side only) to derive the
    ``auth_token_set`` / ``auth_token_masked`` flags, then strip the raw token
    back out before it ever reaches ``_public_provider``.
    """
    row = db.get_model_provider(provider_id, include_token=True)
    if row is None:
        return None
    return _apply_token_flags(row)


# ============================================================================
# Routes
# ============================================================================

@router.get("", response_model=ModelProviderListResponse)
@router.get("/", response_model=ModelProviderListResponse)
async def list_providers(token: str = Depends(require_admin)):
    """List all model providers (public/masked view — no tokens)."""
    rows = db.get_all_model_providers(include_token=True)
    providers = [_public_provider(_apply_token_flags(row)) for row in rows]
    return ModelProviderListResponse(providers=providers)


@router.post("", response_model=ModelProviderResponse)
@router.post("/", response_model=ModelProviderResponse)
async def create_provider(body: ModelProviderCreate, token: str = Depends(require_admin)):
    """Create a new model provider."""
    _validate_provider_type(body.provider_type)
    _validate_base_url(body.base_url, required=True)
    _validate_models(body.models)
    _validate_extra_env(body.extra_env)

    encrypted_token = None
    raw_token = (body.auth_token or "").strip()
    if raw_token:
        encrypted_token = _encrypt_token_or_400(raw_token)

    provider_id = str(uuid.uuid4())

    row = db.create_model_provider(
        provider_id=provider_id,
        name=body.name,
        provider_type=body.provider_type,
        base_url=body.base_url,
        auth_token=encrypted_token,
        models=body.models,
        default_model=body.default_model,
        small_fast_model=body.small_fast_model,
        extra_env=body.extra_env,
        context_window=body.context_window,
        max_output_tokens=body.max_output_tokens,
        enabled=body.enabled,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Could not create model provider")

    full_row = _row_with_token_flags(provider_id)
    return _public_provider(full_row)


@router.patch("/{provider_id}", response_model=ModelProviderResponse)
async def update_provider(
    provider_id: str, body: ModelProviderUpdate, token: str = Depends(require_admin)
):
    """Partially update a model provider. Blank/omitted auth_token = unchanged."""
    existing = db.get_model_provider(provider_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    _validate_provider_type(body.provider_type)
    _validate_base_url(body.base_url, required=False)
    _validate_models(body.models)
    _validate_extra_env(body.extra_env)

    updates: Dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.provider_type is not None:
        updates["provider_type"] = body.provider_type
    if body.base_url is not None:
        updates["base_url"] = body.base_url
    if body.models is not None:
        updates["models_json"] = body.models
    if body.default_model is not None:
        updates["default_model"] = body.default_model
    if body.small_fast_model is not None:
        updates["small_fast_model"] = body.small_fast_model
    if body.extra_env is not None:
        updates["extra_env_json"] = body.extra_env
    if body.context_window is not None:
        updates["context_window"] = body.context_window
    if body.max_output_tokens is not None:
        updates["max_output_tokens"] = body.max_output_tokens
    if body.enabled is not None:
        updates["enabled"] = body.enabled

    # Blank or omitted auth_token = leave the stored token untouched.
    raw_token = (body.auth_token or "").strip()
    if raw_token:
        updates["auth_token"] = _encrypt_token_or_400(raw_token)

    if updates:
        row = db.update_model_provider(provider_id, **updates)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Model provider not found")

    full_row = _row_with_token_flags(provider_id)
    return _public_provider(full_row)


@router.delete("/{provider_id}")
async def delete_provider(provider_id: str, token: str = Depends(require_admin)):
    """Delete a model provider."""
    deleted = db.delete_model_provider(provider_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")
    return {"success": True}


@router.post("/{provider_id}/toggle", response_model=ModelProviderResponse)
async def toggle_provider(provider_id: str, token: str = Depends(require_admin)):
    """Flip a model provider's ``enabled`` flag."""
    existing = db.get_model_provider(provider_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    row = db.set_model_provider_enabled(provider_id, not bool(existing.get("enabled")))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    full_row = _row_with_token_flags(provider_id)
    return _public_provider(full_row)
