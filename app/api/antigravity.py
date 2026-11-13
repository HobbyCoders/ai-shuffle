"""Google Antigravity provider rows: install, status, sign-in and the guard.

The agent is Google's own ``agy`` binary (app/core/antigravity.py). These
endpoints never return or accept the binary's stored sign-in; sign-in runs
the binary's own terminal flow and only its link and the owner's pasted code
pass through here.
"""

from __future__ import annotations

import functools
import logging
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.auth import require_admin
from app.core import antigravity
from app.db import database as db
from app.db.async_database import run_sync
from app.core.provider_locks import operation_lock

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/antigravity", tags=["Antigravity"])

#: Models seeding the picker (kept in app/core/antigravity.py with their thinking levels).
DEFAULT_MODELS: List[Dict[str, Any]] = antigravity.DEFAULT_MODELS

_AUTH_MODES = [
    {"id": "oauth-personal", "label": "Personal Google sign-in", "headless": True,
     "guarded": True, "warning": True},
    {"id": "oauth-business", "label": "Business Google sign-in", "headless": False,
     "guarded": False, "warning": False},
    {"id": "gemini-api-key", "label": "Gemini API key", "headless": True,
     "guarded": False, "warning": False},
]


class AntigravityProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    auth_mode: Literal["oauth-personal", "oauth-business", "gemini-api-key"]
    api_key: Optional[str] = Field(default=None, max_length=4096)
    models: Optional[List[Dict[str, Any]]] = None
    default_model: Optional[str] = None
    enabled: bool = True


class GuardUpdate(BaseModel):
    allow_unattended: bool


class LoginComplete(BaseModel):
    code: str = Field(min_length=1, max_length=4096)


async def _row(provider_id: str, *, include_token: bool = False) -> Dict[str, Any]:
    row = await run_sync(db.get_model_provider, provider_id, include_token=include_token)
    if row is None or row.get("provider_type") != antigravity.PROVIDER_TYPE:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Antigravity provider not found")
    return row


def _guard_view(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "guard_applies": row.get("auth_mode") in antigravity.GUARDED_AUTH_MODES,
        "allow_unattended": antigravity.guard_lifted(row),
        "guarded": antigravity.guarded(row),
    }


@router.get("/meta")
async def meta(token: str = Depends(require_admin)):
    """Static facts the provider screens show."""
    return {
        "provider_type": antigravity.PROVIDER_TYPE,
        "capability": antigravity.CAPABILITY,
        "auth_modes": _AUTH_MODES,
        "warning": antigravity.WARNING_TEXT,
        "terms_url": antigravity.TERMS_URL,
        "dashboard_url": antigravity.DASHBOARD_URL,
        "guard_default": True,
        "default_models": DEFAULT_MODELS,
    }


@router.get("/status")
async def install_status(token: str = Depends(require_admin)):
    """Whether this worker has the binary, its version, and peers lacking support.

    ``source``: ``app`` (installed here by AI Shuffle), ``path`` (an ``agy`` on
    PATH), ``override`` (``AI_SHUFFLE_ANTIGRAVITY_BIN``) or null. ``can_install``:
    Google publishes a build for this worker's platform. ``install``: the
    install or update job (``kind``, ``state``, ``version``, ``message``, ``error``).
    """
    binary, source = await run_sync(antigravity.locate_binary)
    version = await antigravity.binary_version(binary) if binary else None
    return {
        "installed": bool(binary and version),
        "version": version,
        "source": source if binary and version else None,
        "can_install": antigravity.manifest_key() is not None,
        "install": antigravity.install_state(),
        "workers_requiring_update": await run_sync(antigravity.workers_requiring_update),
    }


@router.post("/install")
async def install(token: str = Depends(require_admin)):
    """Download Google's current release for this worker (Windows, macOS or
    Linux, x64 or arm64; sha512-checked) in the background."""
    return antigravity.start_install()


@router.post("/update")
async def update(token: str = Depends(require_admin)):
    """Check for update: the same background job. Says the agent is up to date,
    or installs Google's newer release and switches to it; poll ``GET /status``."""
    return antigravity.start_update()


@router.get("/providers")
async def list_providers(token: str = Depends(require_admin)):
    """This leg's rows (public view, no keys) with their guard state.

    The same rows also appear in ``GET /api/v1/model-providers``.
    """
    from app.api.model_providers import _apply_token_flags, _public_provider

    rows = await run_sync(db.get_all_model_providers, include_token=True)
    return {"providers": [
        {**_public_provider(_apply_token_flags(row)).model_dump(), **_guard_view(row)}
        for row in rows if row.get("provider_type") == antigravity.PROVIDER_TYPE
    ]}


@router.post("/providers")
async def create_provider(body: AntigravityProviderCreate, token: str = Depends(require_admin)):
    """Create an Antigravity row once every member worker can run one."""
    from app.api.model_providers import _encrypt_token_or_400, _public_provider, _row_with_token_flags

    try:
        missing = await run_sync(antigravity.workers_requiring_update)
    except Exception:
        logger.exception("Could not check which workers support Antigravity")
        raise HTTPException(status_code=503, detail=(
            "Could not check whether every worker is up to date. Try again in a moment."))
    if missing:
        raise HTTPException(status_code=409, detail={
            "message": "Update these workers before adding an Antigravity provider: " + ", ".join(missing),
            "workers": missing,
        })
    key = (body.api_key or "").strip()
    if body.auth_mode == "gemini-api-key" and not key:
        raise HTTPException(status_code=400, detail="A Gemini API key is required.")
    if body.auth_mode != "gemini-api-key" and key:
        raise HTTPException(status_code=400, detail="This sign-in kind takes no key.")
    provider_id = str(uuid.uuid4())
    models = body.models if body.models is not None else DEFAULT_MODELS
    async with operation_lock(provider_id):
        row = await run_sync(
            db.create_model_provider,
            provider_id=provider_id,
            name=body.name.strip(),
            provider_type=antigravity.PROVIDER_TYPE,
            # No HTTP endpoint: the agent binary is the only thing that talks to Google.
            base_url="",
            auth_mode=body.auth_mode,
            auth_token=_encrypt_token_or_400(key) if key else None,
            models=models,
            default_model=body.default_model or (models[0]["id"] if models else None),
            enabled=body.enabled,
        )
    if row is None:
        raise HTTPException(status_code=500, detail="Could not create the provider")
    return {**_public_provider(await run_sync(_row_with_token_flags, provider_id)).model_dump(),
            **_guard_view(row)}


@router.get("/providers/{provider_id}/status")
async def provider_status(provider_id: str, token: str = Depends(require_admin)):
    """Ask this worker's binary whether the row can open sessions here.

    ``state``: signed_in | signed_out | not_installed | error. A stored
    sign-in lives only on the worker where it was made. ``login``: a sign-in
    waiting for its code (started on any device), or null.
    """
    row = await _row(provider_id, include_token=True)
    result = await antigravity.probe(row)
    return {**result, **_guard_view(row), "auth_mode": row.get("auth_mode"),
            "login": antigravity.pending_login(provider_id)}


@router.put("/providers/{provider_id}/guard")
async def set_guard(provider_id: str, body: GuardUpdate, token: str = Depends(require_admin)):
    """Lift or restore the interactive-only guard of a personal sign-in row."""
    # Read, change and write under the row's lock, like every other row
    # writer, and only over the revision read (a synced edit from another
    # worker takes no local lock): a concurrent edit is never lost.
    async with operation_lock(provider_id):
        for _attempt in range(3):
            row = await _row(provider_id)
            if row.get("auth_mode") not in antigravity.GUARDED_AUTH_MODES:
                raise HTTPException(status_code=400, detail="Only personal sign-in rows have this guard.")
            env = dict(row.get("extra_env_json") or {}) if isinstance(row.get("extra_env_json"), dict) else {}
            env.pop(antigravity.GUARD_OVERRIDE_KEY, None)
            if body.allow_unattended:
                env[antigravity.GUARD_OVERRIDE_KEY] = "1"
            updated = await run_sync(functools.partial(
                db.update_model_provider, provider_id, expected_revision=row.get("revision"),
                extra_env_json=env))
            if updated is not None or row.get("revision") is None:
                break
        else:
            raise HTTPException(status_code=409, detail="The provider changed on another worker. Try again.")
    if updated is None:
        raise HTTPException(status_code=404, detail="Antigravity provider not found")
    return _guard_view(updated)


@router.post("/providers/{provider_id}/login")
async def login_start(provider_id: str, token: str = Depends(require_admin)):
    """Start the binary's own sign-in; returns the link the owner opens.

    ``state``: awaiting_code (open ``url``, then complete with the code) |
    failed | not_installed | signed_in (already signed in here: nothing was
    started; sign out first to change accounts).
    """
    row = await _row(provider_id)
    return await antigravity.start_login(row)


@router.post("/providers/{provider_id}/login/{login_id}/complete")
async def login_complete(provider_id: str, login_id: str, body: LoginComplete,
                         token: str = Depends(require_admin)):
    """Hand the pasted code (or landing URL) to the binary; signed_in | failed."""
    row = await _row(provider_id)
    login = antigravity.get_login(login_id)
    if login is None or login.row_id != provider_id:
        raise HTTPException(status_code=404, detail="This sign-in expired. Start it again.")
    return await antigravity.complete_login(login, body.code, row)


@router.delete("/providers/{provider_id}/login/{login_id}")
async def login_cancel(provider_id: str, login_id: str, token: str = Depends(require_admin)):
    login = antigravity.get_login(login_id)
    if login is not None and login.row_id == provider_id:
        await antigravity.cancel_login(login_id)
    return {"cancelled": True}


@router.post("/providers/{provider_id}/sign-out")
async def sign_out(provider_id: str, token: str = Depends(require_admin)):
    """End this worker's stored sign-in for the row (removes its private home)."""
    row = await _row(provider_id)
    await antigravity.sign_out(row)
    return {"state": "signed_out"}
