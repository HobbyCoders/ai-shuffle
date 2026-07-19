"""API routes for the model-provider registry (non-Anthropic gateways).

Admin-gated CRUD for the ``model_providers`` table: each row is a gateway
(OpenRouter or a custom Anthropic-compatible endpoint) whose env vars get
injected into a spawned Claude Code session (see ``app/core/environment.py``,
Chunk 3). Mounted under ``/api/v1/model-providers``.

The stored ``auth_token`` (an ``enc:``-prefixed Fernet blob) is NEVER returned
in a response body — only ``auth_token_set`` (bool) and ``auth_token_masked``
(via ``mask_api_key``, same convention as ``IntegrationSettingsResponse``).

The Codex/ChatGPT-subscription OAuth blobs (``codex_access_token`` /
``codex_refresh_token``) get the same treatment: never in a response body, only
the boolean ``codex_access_token_set`` / ``codex_refresh_token_set`` flags. The
non-secret Codex fields (``auth_mode``, ``codex_account_id``, ``needs_reauth``)
are exposed directly. This chunk has NO write path for the token blobs — they
are populated by the (later) device-code enrollment flow + refresh worker.
"""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.auth import require_admin
from app.api.settings_crypto import mask_api_key
from app.core import codex_oauth, encryption, model_catalog_presets
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
    # Codex/ChatGPT-subscription OAuth surface (blobs never exposed — only flags).
    auth_mode: Optional[str] = None
    codex_account_id: Optional[str] = None
    needs_reauth: bool = False
    codex_access_token_set: bool = False
    codex_refresh_token_set: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ModelProviderListResponse(BaseModel):
    providers: List[ModelProviderResponse]


class FetchModelsResponse(BaseModel):
    """Result of a catalog fetch for a provider.

    ``source`` is ``"preset"`` for a ChatGPT/Codex provider (built-in roster, no
    network) or ``"upstream"`` for a live ``GET {base_url}/models`` fetch. The UI
    check-selects from ``models`` and PUTs the curated subset via the normal
    update route — this endpoint never persists anything.
    """
    source: str
    models: List[Dict[str, Any]]


class CodexDeviceStartResponse(BaseModel):
    """Device-code enrollment start: the admin enters ``user_code`` at
    ``verification_uri`` in their own browser, then the frontend polls."""
    user_code: str
    verification_uri: str


class CodexDevicePollResponse(BaseModel):
    """One poll step. ``status`` is ``pending`` (keep polling), ``slow_down``
    (keep polling, but slower), or ``complete`` (``provider`` carries the fresh
    public row)."""
    status: str
    provider: Optional[ModelProviderResponse] = None


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


def _iso(value: Any) -> Optional[str]:
    """Timestamps arrive as ``str`` from the SQLite shim but ``datetime`` from
    real Postgres — coerce to an ISO string either way."""
    if value is None or isinstance(value, str):
        return value
    return value.isoformat()


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
        auth_mode=row.get("auth_mode"),
        codex_account_id=row.get("codex_account_id"),
        needs_reauth=bool(row.get("needs_reauth")),
        codex_access_token_set=bool(row.get("_codex_access_token_set")),
        codex_refresh_token_set=bool(row.get("_codex_refresh_token_set")),
        created_at=_iso(row.get("created_at")),
        updated_at=_iso(row.get("updated_at")),
    )


def _apply_token_flags(row: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the raw token blobs off a row and derive the public ``_set`` flags
    in their place. Mutates and returns the same row.

    Covers ``auth_token`` (``_auth_token_set`` / ``_auth_token_masked``) and the
    two Codex OAuth blobs ``codex_access_token`` / ``codex_refresh_token``
    (``_codex_access_token_set`` / ``_codex_refresh_token_set``). The raw blobs
    never survive this call, so they can never reach ``_public_provider``.
    """
    token = row.pop("auth_token", None)
    row["_auth_token_set"] = bool(token)
    row["_auth_token_masked"] = mask_api_key(token) if token else ""
    codex_access = row.pop("codex_access_token", None)
    codex_refresh = row.pop("codex_refresh_token", None)
    row["_codex_access_token_set"] = bool(codex_access)
    row["_codex_refresh_token_set"] = bool(codex_refresh)
    return row


# Upstream catalog fetch (D.1) — cap so a pathological gateway can't return an
# unbounded list into the picker. Timeout mirrors the codex OAuth calls.
_FETCH_MODELS_CAP = 2000
_FETCH_TIMEOUT = 30.0


class UpstreamModelsError(Exception):
    """Upstream ``/models`` fetch failed (non-2xx or transport error). The route
    maps this to a 502 — the gateway, not our request, is at fault."""


def _parse_models_response(payload: Any) -> List[Dict[str, Any]]:
    """Normalize an upstream ``/models`` body into ``[{id, label}]``.

    Handles both OpenAI-style ``{"data": [{"id": ...}]}`` and a bare list of
    entries. Each entry may be an object (``id`` required; ``label``/``name``
    optional, falling back to ``id``) or a bare string id. Entries without a
    usable id are skipped. The list is capped at ``_FETCH_MODELS_CAP``.
    """
    if isinstance(payload, dict):
        raw = payload.get("data")
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    models: List[Dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, str):
            model_id = entry
            label = entry
        elif isinstance(entry, dict):
            model_id = entry.get("id") or entry.get("model")
            if not isinstance(model_id, str) or not model_id:
                continue
            label = entry.get("label") or entry.get("name") or model_id
            if not isinstance(label, str) or not label:
                label = model_id
        else:
            continue
        if not model_id:
            continue
        models.append({"id": model_id, "label": label})
        if len(models) >= _FETCH_MODELS_CAP:
            break
    return models


async def fetch_upstream_models(
    base_url: str,
    token: Optional[str],
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> List[Dict[str, Any]]:
    """Fetch + normalize a provider's ``GET {base_url}/models`` catalog.

    Injectable ``client`` (an ``httpx.AsyncClient`` over a ``MockTransport`` in
    tests) mirrors the ``app.core.codex_oauth`` convention; production passes the
    shared client. The decrypted ``token`` (when present) rides as
    ``Authorization: Bearer``. Raises :class:`UpstreamModelsError` on any non-2xx
    or transport failure.
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=_FETCH_TIMEOUT)
    try:
        resp = await client.get(url, headers=headers, timeout=_FETCH_TIMEOUT)
    except httpx.HTTPError as e:
        raise UpstreamModelsError(str(e)) from e
    finally:
        if owns_client and client is not None:
            await client.aclose()

    if resp.status_code >= 400:
        raise UpstreamModelsError(f"http_{resp.status_code}")
    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 — malformed upstream body
        raise UpstreamModelsError("invalid_json") from e
    return _parse_models_response(payload)


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
    """Delete a model provider.

    Best-effort: if the provider carries a stored Codex refresh token, revoke it
    upstream BEFORE deletion so we don't leave a live grant dangling. A revoke
    failure NEVER blocks deletion (the credential row is going away regardless).
    """
    await _best_effort_revoke_codex(provider_id)
    deleted = db.delete_model_provider(provider_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")
    return {"success": True}


async def _best_effort_revoke_codex(provider_id: str) -> None:
    """Revoke a stored Codex refresh token (if any) before provider delete.

    Swallows every error — deletion must succeed even if revoke can't (network
    down, encryption locked, token already dead).
    """
    try:
        stored = db.get_codex_tokens(provider_id)
        if not stored:
            return
        enc_refresh = stored.get("codex_refresh_token")
        if not enc_refresh:
            return
        if not encryption.is_encryption_ready():
            return
        refresh_plain = encryption.decrypt_value(enc_refresh)
        await codex_oauth.revoke_token(refresh_plain)
    except Exception:  # noqa: BLE001 — revoke is best-effort, never blocks delete
        logger.debug("codex revoke-on-delete failed for %s", provider_id, exc_info=True)


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


@router.post("/{provider_id}/fetch-models", response_model=FetchModelsResponse)
async def fetch_models(provider_id: str, token: str = Depends(require_admin)):
    """Fetch a provider's model catalog for the curation UI (phase-2 Chunk D.1/D.2).

    Two sources, one endpoint:
      * ChatGPT/Codex provider (``auth_mode == "chatgpt"``) → the built-in preset
        roster, no network call (the roster is server-enforced upstream, F3).
      * Any other provider → live ``GET {base_url}/models`` with the provider's
        decrypted auth token as ``Authorization: Bearer``.

    This route RETURNS the list only — the admin check-selects and persists the
    curated subset through the existing ``PATCH /{id}`` update route. There is no
    second write path here.
    """
    row = db.get_model_provider(provider_id, include_token=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    # Preset path: ChatGPT/Codex subscription — no browsable /models endpoint.
    preset = model_catalog_presets.preset_models_for(row)
    if preset is not None:
        return FetchModelsResponse(source="preset", models=preset)

    # Upstream path: decrypt the stored token (if any) and fetch live.
    enc_token = row.get("auth_token")
    auth_token: Optional[str] = None
    if enc_token:
        if not encryption.is_encryption_ready():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Encryption is locked. Log in again as admin (or set "
                       "ADMIN_PASSWORD) before fetching provider models.",
            )
        auth_token = encryption.decrypt_value(enc_token)

    from app.core.http_client import get_http_client
    try:
        models = await fetch_upstream_models(
            row["base_url"], auth_token, client=get_http_client()
        )
    except UpstreamModelsError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch models from the provider upstream: {e}",
        )
    return FetchModelsResponse(source="upstream", models=models)


# ============================================================================
# Codex / ChatGPT-subscription device-code OAuth enrollment (phase-2 Chunk B.2)
# ============================================================================
#
# Two-step, admin-driven device-code flow (plan B RULING: device-code, not the
# loopback-1455 flow — the admin's browser is remote from the server):
#   1. ``/device/start`` mints PKCE + a user-code; the admin types it into their
#      own browser at the verification URL.
#   2. ``/device/poll`` is called repeatedly by the frontend; on authorization it
#      encrypts + persists the token bundle and returns the fresh public row.
#
# The PKCE ``code_verifier`` + ``device_code`` are pending-enrollment state that
# MUST survive between the two calls but must NOT go to the browser (the verifier
# is the PKCE secret). We keep them in a small in-memory map keyed by
# provider_id. One backend process serves each install's CLI (per the
# primary/worker split), so process-local state is sufficient; a lost map (e.g.
# restart mid-enrollment) just means the admin restarts the flow.

# provider_id -> (device_code, code_verifier, created_at_monotonic)
_pending_enrollments: Dict[str, Tuple[str, str, float]] = {}
# Drop pending enrollments older than this (device codes expire upstream anyway).
_PENDING_TTL_SECONDS = 900


def _prune_pending() -> None:
    now = time.monotonic()
    stale = [
        pid for pid, (_, _, ts) in _pending_enrollments.items()
        if now - ts > _PENDING_TTL_SECONDS
    ]
    for pid in stale:
        _pending_enrollments.pop(pid, None)


@router.post("/{provider_id}/codex/device/start",
             response_model=CodexDeviceStartResponse)
async def codex_device_start(provider_id: str, token: str = Depends(require_admin)):
    """Start the Codex device-code enrollment for a provider.

    Returns the ``user_code`` the admin enters at ``verification_uri`` in their
    own browser. Stashes the PKCE verifier + device_code server-side for the
    subsequent poll(s).
    """
    if not encryption.is_encryption_ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Encryption is locked. Log in again as admin (or set "
                   "ADMIN_PASSWORD) before enrolling a Codex login.",
        )
    if db.get_model_provider(provider_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    try:
        start = await codex_oauth.start_device_code()
    except codex_oauth.CodexAuthError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Codex device-code start failed: {e.code}")

    _prune_pending()
    _pending_enrollments[provider_id] = (
        start.device_code, start.code_verifier, time.monotonic()
    )
    return CodexDeviceStartResponse(
        user_code=start.user_code,
        verification_uri=start.verification_uri,
    )


@router.post("/{provider_id}/codex/device/poll",
             response_model=CodexDevicePollResponse)
async def codex_device_poll(provider_id: str, token: str = Depends(require_admin)):
    """One poll step of the Codex device-code enrollment.

    ``pending`` / ``slow_down`` → keep polling. On authorization: encrypt the
    access+refresh blobs (same call-site convention as ``auth_token``), persist
    the whole bundle atomically via ``set_codex_token_bundle`` (which sets
    ``auth_mode='chatgpt'`` and clears ``needs_reauth``), and return the fresh
    public row. Terminal upstream errors surface as 400.
    """
    pending = _pending_enrollments.get(provider_id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending Codex enrollment for this provider — start one first.",
        )
    device_code, code_verifier, _ = pending

    try:
        bundle = await codex_oauth.poll_device_code(device_code, code_verifier)
    except codex_oauth.CodexAuthPending as pend:
        return CodexDevicePollResponse(
            status="slow_down" if pend.slow_down else "pending"
        )
    except codex_oauth.CodexAuthError as e:
        _pending_enrollments.pop(provider_id, None)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Codex enrollment failed: {e.code}")

    # Authorized — encrypt the secrets (same seam as auth_token) and persist.
    enc_access = _encrypt_token_or_400(bundle.access_token)
    enc_refresh = _encrypt_token_or_400(bundle.refresh_token)
    exp_dt = _codex_id_token_exp(bundle)
    id_token_exp = exp_dt.isoformat() if exp_dt is not None else None

    from datetime import datetime, timezone
    row = db.set_codex_token_bundle(
        provider_id,
        access_token=enc_access,
        refresh_token=enc_refresh,
        account_id=bundle.account_id,
        id_token_expires_at=id_token_exp,
        last_refresh=datetime.now(timezone.utc).isoformat(),
        needs_reauth=False,
    )
    _pending_enrollments.pop(provider_id, None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    full_row = _row_with_token_flags(provider_id)
    return CodexDevicePollResponse(status="complete", provider=_public_provider(full_row))


def _codex_id_token_exp(bundle: "codex_oauth.TokenBundle"):
    """Best-effort ``exp`` from the enrolled id_token (falls back to the access
    token) so the stored ``codex_id_token_expires_at`` reflects real expiry."""
    from app.core import codex_token_refresh
    return codex_token_refresh._access_token_exp(bundle.id_token or bundle.access_token)


@router.post("/{provider_id}/codex/clear-reauth",
             response_model=ModelProviderResponse)
async def codex_clear_reauth(provider_id: str, token: str = Depends(require_admin)):
    """Clear ONLY the ``needs_reauth`` flag on a Codex provider.

    Preserves the stored token bundle unchanged (re-supplies the existing
    encrypted blobs); flips just the flag. Used to dismiss a stale reconnect
    prompt without re-enrolling.
    """
    stored = db.get_codex_tokens(provider_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")
    row = db.set_codex_token_bundle(
        provider_id,
        access_token=stored.get("codex_access_token"),
        refresh_token=stored.get("codex_refresh_token"),
        account_id=stored.get("codex_account_id"),
        id_token_expires_at=stored.get("codex_id_token_expires_at"),
        last_refresh=stored.get("codex_last_refresh"),
        needs_reauth=False,
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")
    full_row = _row_with_token_flags(provider_id)
    return _public_provider(full_row)
