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

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator

from app.api.auth import require_admin, require_auth
from app.api.settings_crypto import mask_api_key
from app.core import (
    codex_oauth,
    codex_rate_limit_resets,
    codex_token_refresh,
    encryption,
    grok_rate_limit_resets,
    model_catalog_presets,
    model_modality,
    model_registry,
    xai_oauth,
)
from app.db import database as db

from app.api import openai_billing

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/model-providers", tags=["Model Providers"])
router.include_router(openai_billing.router)

_VALID_PROVIDER_TYPES = ("openrouter", "custom", "openai", "chatgpt", "xai", "grok")

# Wire formats a CUSTOM row's endpoint may speak. 'anthropic' (the default when
# unset — the historic custom-row contract: Anthropic Messages at
# {base_url}/v1/messages) or 'openai' (OpenAI chat/completions, for
# xAI-direct / Groq / vLLM style endpoints). Ignored on non-custom rows, whose
# provider_type already pins the dialect.
_VALID_API_DIALECTS = ("anthropic", "openai")


# ============================================================================
# Pydantic models
# ============================================================================

class ModelProviderCreate(BaseModel):
    """Request body to create a model provider."""
    name: str
    provider_type: str
    base_url: str
    api_dialect: Optional[str] = None
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
    api_dialect: Optional[str] = None
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
    api_dialect: Optional[str] = None
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
    openai_billing_org_id: Optional[str] = None
    openai_billing_instance_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ModelProviderListResponse(BaseModel):
    providers: List[ModelProviderResponse]


class FetchModelsResponse(BaseModel):
    """Result of a catalog fetch for a provider.

    ``source`` is ``"upstream"`` for a live catalog pull. ChatGPT/Codex hits
    ``GET https://chatgpt.com/backend-api/codex/models``; every other provider
    hits its OpenAI-compatible ``/models`` URL. ``"preset"`` is retained only
    so older clients still parse the field. The UI check-selects from
    ``models`` and PATCHes the curated subset via the normal update route —
    this endpoint never persists anything.

    ``models`` carries TEXT-generation models only. Audio, voice, image and
    video endpoints are split into ``hidden`` (each entry keeps its shape, plus
    a ``media_kind``) so the UI can say where they live instead of leaving the
    user hunting for a model their key genuinely offers. Defaulted, so an older
    client that ignores it still parses.
    """
    source: str
    models: List[Dict[str, Any]]
    hidden: List[Dict[str, Any]] = []


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


class CodexResetCreditResponse(BaseModel):
    """Public, normalized view of one ChatGPT/Codex or Grok reset credit."""

    id: str
    reset_type: Literal["codex_rate_limits", "grok_rate_limits", "unknown"]
    status: Literal["available", "redeeming", "redeemed", "unknown"]
    granted_at: int
    expires_at: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None


class CodexResetCreditsResponse(BaseModel):
    available_count: int
    credits: List[CodexResetCreditResponse]


class CodexConsumeResetCreditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_id: str
    idempotency_key: str

    @field_validator("credit_id", "idempotency_key")
    @classmethod
    def _must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class CodexConsumeResetCreditResponse(BaseModel):
    outcome: Literal[
        "reset", "nothing_to_reset", "no_credit", "already_redeemed"
    ]
    windows_reset: int


# ============================================================================
# Validation helpers
# ============================================================================

def _validate_provider_type(provider_type: Optional[str]) -> None:
    if provider_type is not None and provider_type not in _VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"provider_type must be one of {_VALID_PROVIDER_TYPES}",
        )


def _validate_api_dialect(api_dialect: Optional[str]) -> None:
    if api_dialect is not None and api_dialect not in _VALID_API_DIALECTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"api_dialect must be one of {_VALID_API_DIALECTS}",
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


def _normalize_base_url(provider_type: Optional[str], base_url: Optional[str]) -> Optional[str]:
    """OpenRouter's canonical base is https://openrouter.ai/api (the CLI appends
    /v1/messages). Older UI bundles seeded .../api/v1 — strip a trailing /v1 on
    openrouter rows so stale enrollments self-heal. Other types stay verbatim."""
    if provider_type != "openrouter" or not isinstance(base_url, str) or not base_url:
        return base_url
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed


def _encrypt_token_or_400(token: str) -> str:
    """Encrypt a non-blank auth token, or raise 400 if encryption isn't ready."""
    if not encryption.is_encryption_ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credential storage is locked; local recovery is required.",
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
        api_dialect=row.get("api_dialect"),
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
        openai_billing_org_id=row.get("openai_billing_org_id"),
        openai_billing_instance_id=row.get("openai_billing_instance_id"),
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
_CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
_CODEX_CLIENT_VERSION = "1.0.0"
# Special-purpose Codex review model — not a general chat/agent option.
_CODEX_EXCLUDED_MODEL_IDS = frozenset({"codex-auto-review"})
_CATALOG_RECONNECT_CHATGPT = (
    "Reconnect the ChatGPT/Codex provider before fetching models."
)
_CATALOG_RECONNECT_GROK = (
    "Reconnect the Grok provider before fetching models."
)


class UpstreamModelsError(Exception):
    """Upstream ``/models`` fetch failed (non-2xx or transport error). The route
    maps this to a 502 — the gateway, not our request, is at fault."""


def _upstream_context_length(entry: Dict[str, Any]) -> Optional[int]:
    """Per-model context length advertised by an upstream catalog entry.

    OpenRouter (and the gateways that copy its shape) publish it as
    ``context_length`` at the top level and/or under ``top_provider``; the
    latter is the more specific value (the actual serving provider's window) so
    it wins. Codex publishes ``context_window`` instead. Returns ``None`` when
    absent or unusable — never a guessed default.
    """
    candidates = []
    top_provider = entry.get("top_provider")
    if isinstance(top_provider, dict):
        candidates.append(top_provider.get("context_length"))
    candidates.append(entry.get("context_length"))
    candidates.append(entry.get("context_window"))
    for value in candidates:
        if value is None or isinstance(value, bool):
            continue
        try:
            window = int(value)
        except (TypeError, ValueError):
            continue
        if window > 0:
            return window
    return None


def _parse_models_response(
    payload: Any,
    *,
    provider_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Normalize an upstream ``/models`` body into ``[{id, label}]``.

    Handles OpenAI-style ``{"data": [{"id": ...}]}``, Codex-style
    ``{"models": [{"slug": ...}]}`` (also ``id`` / ``model``), and a bare list
    of entries. Each entry may be an object (``id`` / ``model`` / ``slug``
    required; ``label`` / ``name`` / ``display_name`` optional, falling back to
    the id) or a bare string id. Entries without a usable id are skipped.
    The list is capped at ``_FETCH_MODELS_CAP``.

    An upstream-advertised per-model window (``context_length`` /
    ``top_provider.context_length`` / Codex ``context_window``) is CARRIED
    THROUGH as ``context_window``.
    It used to be dropped here, which forced every model on the row to share the
    row's single ``context_window`` column — wrong the moment one model on the
    row has a different window.

    OpenRouter catalog names (``Anthropic: Claude Fable 5.1``) have the author
    prefix stripped when ``provider_type`` is ``openrouter`` — see
    ``display_provider_model_label``.
    """
    if isinstance(payload, dict):
        raw = payload.get("data")
        if not isinstance(raw, list):
            raw = payload.get("models")
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    models: List[Dict[str, Any]] = []
    for entry in raw:
        context_window: Optional[int] = None
        if isinstance(entry, str):
            model_id = entry
            label = entry
        elif isinstance(entry, dict):
            model_id = entry.get("id") or entry.get("model") or entry.get("slug")
            if not isinstance(model_id, str) or not model_id:
                continue
            label = (
                entry.get("label")
                or entry.get("name")
                or entry.get("display_name")
                or model_id
            )
            if not isinstance(label, str) or not label:
                label = model_id
            context_window = _upstream_context_length(entry)
        else:
            continue
        if not model_id:
            continue
        label = model_registry.display_provider_model_label(label, provider_type)
        model: Dict[str, Any] = {"id": model_id, "label": label}
        if context_window is not None:
            model["context_window"] = context_window
        # OpenRouter publishes per-token prices here. Preserve the numeric
        # strings for pre-call spend projection; never copy arbitrary catalog
        # metadata into the persisted provider row.
        if isinstance(entry, dict) and isinstance(entry.get("pricing"), dict):
            pricing = entry["pricing"]
            kept = {
                key: pricing[key]
                for key in ("prompt", "completion", "input_cache_read", "input_cache_write")
                if isinstance(pricing.get(key), (str, int, float))
            }
            if kept:
                model["pricing"] = kept
        from app.core.model_effort import sanitize_effort_metadata

        if isinstance(entry, dict):
            metadata = sanitize_effort_metadata(entry, provider_type=provider_type, complete=True)
            if provider_type != "openrouter" and not sanitize_effort_metadata(entry):
                from app.core.model_effort import effort_capabilities
                official = effort_capabilities(model_id, {"provider_type": provider_type})
                if official["effort_levels"] is not None:
                    metadata = {"reasoning_capabilities": {
                        "version": 1, "effort_levels": official["effort_levels"],
                        "default_effort": official["default_effort"],
                    }}
            model.update(metadata)
        models.append(model)
        if len(models) >= _FETCH_MODELS_CAP:
            break
    return models


def _models_catalog_url(base_url: str) -> str:
    """OpenAI-compatible models URL without doubling an already-versioned ``/v1``.

    OpenAI/xAI rows store ``https://api.openai.com/v1`` (already versioned);
    OpenRouter/custom rows store a host like ``https://openrouter.ai/api``.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        raise UpstreamModelsError("missing_base_url")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def _codex_catalog_headers(access_token: str, account_id: Optional[str]) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": codex_rate_limit_resets.CODEX_USER_AGENT,
        "originator": "codex_cli_rs",
        "OpenAI-Beta": "responses=experimental",
        "Accept": "application/json",
    }
    if isinstance(account_id, str) and account_id.strip():
        headers["chatgpt-account-id"] = account_id
    return headers


def _subscription_access_token(
    provider_id: str, *, kind: str
) -> Tuple[str, Optional[str]]:
    """Decrypt the OAuth access token for a ChatGPT or SuperGrok row."""
    reconnect = (
        _CATALOG_RECONNECT_CHATGPT if kind == "chatgpt" else _CATALOG_RECONNECT_GROK
    )
    if not encryption.is_encryption_ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credential storage is locked; local recovery is required.",
        )
    stored = db.get_codex_tokens(provider_id)
    if (
        not stored
        or stored.get("needs_reauth")
        or not stored.get("codex_access_token")
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reconnect)
    try:
        access = encryption.decrypt_value(stored["codex_access_token"])
    except Exception as exc:  # noqa: BLE001 — never expose credential failures
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=reconnect
        ) from exc
    if not access:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reconnect)
    return access, stored.get("codex_account_id")


async def fetch_upstream_models(
    base_url: str,
    token: Optional[str],
    *,
    client: Optional[httpx.AsyncClient] = None,
    provider_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch + normalize a provider's OpenAI-compatible ``/models`` catalog.

    Injectable ``client`` (an ``httpx.AsyncClient`` over a ``MockTransport`` in
    tests) mirrors the ``app.core.codex_oauth`` convention; production passes the
    shared client. The decrypted ``token`` (when present) rides as
    ``Authorization: Bearer``. Raises :class:`UpstreamModelsError` on any non-2xx
    or transport failure.
    """
    url = _models_catalog_url(base_url)
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
    return _parse_models_response(payload, provider_type=provider_type)


def _generic_auth_token(row: Dict[str, Any]) -> Optional[str]:
    """Decrypt ``auth_token`` when present; None if the row has no API key."""
    enc_token = row.get("auth_token")
    if not enc_token:
        return None
    if not encryption.is_encryption_ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credential storage is locked; local recovery is required.",
        )
    return encryption.decrypt_value(enc_token)


async def _fetch_codex_models(
    provider_id: str, *, client: httpx.AsyncClient
) -> List[Dict[str, Any]]:
    """Live ``GET /backend-api/codex/models`` with Codex OAuth + one 401 retry."""
    access_token, account_id = _subscription_access_token(
        provider_id, kind="chatgpt"
    )
    params = {"client_version": _CODEX_CLIENT_VERSION}

    async def _once(token: str, acct: Optional[str]) -> httpx.Response:
        try:
            return await client.get(
                _CODEX_MODELS_URL,
                headers=_codex_catalog_headers(token, acct),
                params=params,
                timeout=_FETCH_TIMEOUT,
            )
        except httpx.HTTPError as e:
            raise UpstreamModelsError(str(e)) from e

    resp = await _once(access_token, account_id)
    if resp.status_code == 401:
        try:
            refreshed = await codex_token_refresh.refresh_on_401_once(
                provider_id,
                stale_access_token=access_token,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001 — refresh internals stay private
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_CATALOG_RECONNECT_CHATGPT,
            ) from exc
        if not refreshed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_CATALOG_RECONNECT_CHATGPT,
            )
        access_token, account_id = _subscription_access_token(
            provider_id, kind="chatgpt"
        )
        resp = await _once(access_token, account_id)
        if resp.status_code == 401:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_CATALOG_RECONNECT_CHATGPT,
            )
    if resp.status_code >= 400:
        raise UpstreamModelsError(f"http_{resp.status_code}")
    try:
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 — malformed upstream body
        raise UpstreamModelsError("invalid_json") from e
    models = _parse_models_response(
        _codex_visible_payload(payload), provider_type="chatgpt"
    )
    return [m for m in models if m.get("id") not in _CODEX_EXCLUDED_MODEL_IDS]


def _codex_visible_payload(payload: Any) -> Any:
    """Drop Codex rows marked ``visibility: "hide"`` (gpt-reserve, auto-review).

    The live catalog identifies models with ``slug``, not ``id``. Hidden
    special-purpose rows still arrive in the same list; stripping them here
    keeps them out of the ChatGPT sub picker the same way ``codex-auto-review``
    is excluded by id after parse.
    """
    if not isinstance(payload, dict):
        return payload
    raw = payload.get("models")
    if not isinstance(raw, list):
        return payload
    visible = [
        entry
        for entry in raw
        if not (isinstance(entry, dict) and entry.get("visibility") == "hide")
    ]
    return {**payload, "models": visible}


async def _fetch_upstream_with_grok_retry(
    provider_id: str,
    row: Dict[str, Any],
    token: str,
    *,
    client: httpx.AsyncClient,
) -> List[Dict[str, Any]]:
    """SuperGrok catalog fetch: one 401 → refresh → retry."""
    try:
        return await fetch_upstream_models(
            row["base_url"], token, client=client,
            provider_type=row.get("provider_type"),
        )
    except UpstreamModelsError as exc:
        if str(exc) != "http_401":
            raise
    from app.core import xai_token_refresh
    try:
        refreshed = await xai_token_refresh.refresh_on_401_once(
            provider_id, stale_access_token=token, client=client,
        )
    except Exception as exc:  # noqa: BLE001 — refresh internals stay private
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CATALOG_RECONNECT_GROK,
        ) from exc
    if not refreshed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CATALOG_RECONNECT_GROK,
        )
    access, _ = _subscription_access_token(provider_id, kind="xai")
    try:
        return await fetch_upstream_models(
            row["base_url"], access, client=client,
            provider_type=row.get("provider_type"),
        )
    except UpstreamModelsError as exc:
        if str(exc) == "http_401":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_CATALOG_RECONNECT_GROK,
            ) from exc
        raise


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
    _validate_api_dialect(body.api_dialect)
    _validate_models(body.models)
    _validate_extra_env(body.extra_env)

    encrypted_token = None
    raw_token = (body.auth_token or "").strip()
    if raw_token:
        encrypted_token = _encrypt_token_or_400(raw_token)

    provider_id = str(uuid.uuid4())

    base_url = _normalize_base_url(body.provider_type, body.base_url)

    row = db.create_model_provider(
        provider_id=provider_id,
        name=body.name,
        provider_type=body.provider_type,
        base_url=base_url,
        api_dialect=body.api_dialect,
        auth_token=encrypted_token,
        models=model_modality.split_media_models(body.models)[0],
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
    _validate_api_dialect(body.api_dialect)
    _validate_models(body.models)
    _validate_extra_env(body.extra_env)

    updates: Dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.provider_type is not None:
        updates["provider_type"] = body.provider_type
    if body.base_url is not None:
        effective_type = (
            body.provider_type
            if body.provider_type is not None
            else existing.get("provider_type")
        )
        updates["base_url"] = _normalize_base_url(effective_type, body.base_url)
    if body.api_dialect is not None:
        updates["api_dialect"] = body.api_dialect
    if body.models is not None:
        # Sanitize on write, not just on display: four provider flows persist the
        # whole fetched roster with no user picking, so an unfiltered write is
        # how media ids got into models_json in the first place.
        updates["models_json"], _ = model_modality.split_media_models(body.models)
    if body.default_model is not None:
        updates["default_model"] = body.default_model
    if body.small_fast_model is not None:
        updates["small_fast_model"] = body.small_fast_model
    if body.extra_env is not None:
        updates["extra_env_json"] = body.extra_env
    # NULLABLE columns: an explicit `null` must CLEAR them, an omitted field must
    # leave them alone. `is not None` cannot tell those apart, so a row's window
    # / output cap could be set but never unset from the UI. `model_fields_set`
    # carries exactly that distinction (pydantic v2 records which keys the
    # request body actually contained).
    for field in ("context_window", "max_output_tokens"):
        if field in body.model_fields_set:
            updates[field] = getattr(body, field)
    if body.enabled is not None:
        updates["enabled"] = body.enabled

    # Blank or omitted auth_token = leave the stored token untouched.
    raw_token = (body.auth_token or "").strip()
    if raw_token:
        updates["auth_token"] = _encrypt_token_or_400(raw_token)

    if updates:
        async with openai_billing.openai_billing_browser.operation_lock(provider_id):
            row = db.update_model_provider(provider_id, **updates)
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail="Model provider not found")
            if not row.get("enabled") or not openai_billing._is_openai_platform_row(row):
                await openai_billing.close_provider_billing(provider_id)

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
    async with openai_billing.openai_billing_browser.operation_lock(provider_id):
        await openai_billing.close_provider_billing(provider_id)
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

    async with openai_billing.openai_billing_browser.operation_lock(provider_id):
        row = db.set_model_provider_enabled(provider_id, not bool(existing.get("enabled")))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Model provider not found")
        if not row.get("enabled"):
            await openai_billing.close_provider_billing(provider_id)

    full_row = _row_with_token_flags(provider_id)
    return _public_provider(full_row)


class RefreshCapabilitiesRequest(BaseModel):
    provider_ids: List[str]

    @field_validator("provider_ids")
    @classmethod
    def bounded_ids(cls, value):
        if len(value) > 100 or any(not item or len(item) > 200 for item in value):
            raise ValueError("Supply at most 100 provider ids")
        return list(dict.fromkeys(value))


# Coalesce overlapping hydration requests and throttle retrying stale failures.
# Bounded by existing enabled provider ids, not arbitrary caller input.
_capability_locks: Dict[str, asyncio.Lock] = {}
_capability_results: Dict[str, Tuple[float, bool]] = {}
_CAPABILITY_COOLDOWN = 60.0
_capability_semaphore = asyncio.Semaphore(4)


async def _refresh_provider_capabilities(provider_id: str) -> bool:
    row = db.get_model_provider(provider_id)
    if not row or not row.get("enabled"):
        return False
    lock = _capability_locks.setdefault(provider_id, asyncio.Lock())
    async with lock:
        prior = _capability_results.get(provider_id)
        if prior and time.monotonic() - prior[0] < _CAPABILITY_COOLDOWN:
            return prior[1]
        success = False
        try:
            # Reuse the existing catalog/auth clients, without altering curation.
            async with _capability_semaphore:
                response = await asyncio.wait_for(fetch_models(provider_id, token=""), timeout=_FETCH_TIMEOUT + 5)
            fresh = {m["id"]: m for m in response.models}
            # Re-read after network I/O: preserve edits made while awaiting upstream.
            current = db.get_model_provider(provider_id)
            if not current or not current.get("enabled"):
                return False
            from app.core.model_effort import sanitize_effort_metadata
            merged = []
            complete = True
            for entry in current.get("models_json") or []:
                if isinstance(entry, str):
                    entry = {"id": entry, "label": entry}
                updated = dict(entry)
                incoming = fresh.get(entry.get("id"), {}).get("reasoning_capabilities")
                existing = sanitize_effort_metadata(entry).get("reasoning_capabilities")
                if incoming is None:
                    complete = False
                # An incomplete/unknown declaration must never erase known support.
                if incoming is not None and (incoming.get("effort_levels") is not None
                                             or not existing or existing.get("effort_levels") is None):
                    updated["reasoning_capabilities"] = incoming
                elif existing is not None:
                    updated["reasoning_capabilities"] = existing
                merged.append(updated)
            changed = merged != (current.get("models_json") or [])
            if changed:
                if db.update_model_provider(provider_id, models_json=merged) is None:
                    return False
            # Partial repairs still require clients to rehydrate persisted metadata.
            success = complete or changed
        except Exception:  # Catalog/auth details may contain secrets; expose ids only.
            logger.debug("Capability refresh failed for provider %s", provider_id)
        finally:
            _capability_results[provider_id] = (time.monotonic(), success)
        return success


@router.post("/refresh-capabilities")
async def refresh_capabilities(body: RefreshCapabilitiesRequest, token: str = Depends(require_auth)):
    refreshed, failed = [], []
    # Small concurrency bound across one batch; per-provider locks dedupe batches.
    semaphore = asyncio.Semaphore(4)

    async def refresh(provider_id):
        async with semaphore:
            return await _refresh_provider_capabilities(provider_id)

    ids = list(dict.fromkeys(body.provider_ids))
    results = await asyncio.gather(*(refresh(provider_id) for provider_id in ids))
    for provider_id, success in zip(ids, results):
        (refreshed if success else failed).append(provider_id)
    return {"refreshed": refreshed, "failed": failed}


@router.post("/{provider_id}/fetch-models", response_model=FetchModelsResponse)
async def fetch_models(provider_id: str, token: str = Depends(require_admin)):
    """Fetch a provider's live model catalog for the curation UI.

    Every provider is pulled from upstream on this call (Refresh models):
      * ChatGPT/Codex → ``GET https://chatgpt.com/backend-api/codex/models``
        with the subscription access token (one 401 refresh retry).
      * SuperGrok → ``GET {base_url}/models`` with the Grok access token
        (one 401 refresh retry).
      * OpenAI / xAI API key / OpenRouter / custom → ``GET`` the
        OpenAI-compatible ``/models`` URL with the decrypted API key.

    This route RETURNS the list only — the admin check-selects and persists the
    curated subset through the existing ``PATCH /{id}`` update route. There is no
    second write path here.
    """
    row = db.get_model_provider(provider_id, include_token=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    kind = model_catalog_presets.preset_kind_for(row)
    from app.core.http_client import get_http_client
    client = get_http_client()
    try:
        if kind == "chatgpt":
            models = await _fetch_codex_models(provider_id, client=client)
        elif row.get("auth_mode") == "grok":
            access, _ = _subscription_access_token(provider_id, kind="xai")
            models = await _fetch_upstream_with_grok_retry(
                provider_id, row, access, client=client,
            )
        else:
            auth_token = _generic_auth_token(row)
            models = await fetch_upstream_models(
                row["base_url"], auth_token, client=client,
                provider_type=row.get("provider_type"),
            )
    except HTTPException:
        raise
    except UpstreamModelsError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch models from the provider upstream: {e}",
        )
    # One filter covers all three branches above, and it sits here rather than in
    # _parse_models_response for the same reason the codex exclusion does: that
    # helper is a wire-shape parser with its own unit tests, and curation is a
    # separate concern.
    models, media = model_modality.split_media_models(models)
    hidden = [
        {**entry, "media_kind": model_modality.media_kind(entry.get("id"))}
        for entry in media
    ]
    return FetchModelsResponse(source="upstream", models=models, hidden=hidden)


# ============================================================================
# Codex / ChatGPT-subscription rate-limit reset credits
# ============================================================================

_CODEX_RESET_UPSTREAM_DETAIL = (
    "The ChatGPT/Codex reset-credit service is temporarily unavailable."
)
_CODEX_RESET_RECONNECT_DETAIL = (
    "Reconnect the ChatGPT/Codex provider before using reset credits."
)


def _codex_reset_credentials(provider_id: str) -> Tuple[str, Optional[str]]:
    """Load and decrypt one Codex access token for an immediate request."""
    provider = db.get_model_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model provider not found",
        )
    if not (
        provider.get("auth_mode") == "chatgpt"
        or provider.get("provider_type") == "chatgpt"
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rate-limit reset credits require a ChatGPT/Codex provider.",
        )
    if not encryption.is_encryption_ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credential storage is locked; local recovery is required.",
        )

    stored = db.get_codex_tokens(provider_id)
    if (
        not stored
        or stored.get("needs_reauth")
        or not stored.get("codex_access_token")
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CODEX_RESET_RECONNECT_DETAIL,
        )
    try:
        access_token = encryption.decrypt_value(stored["codex_access_token"])
    except Exception as exc:  # noqa: BLE001 - never expose credential failures
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CODEX_RESET_RECONNECT_DETAIL,
        ) from exc
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CODEX_RESET_RECONNECT_DETAIL,
        )
    return access_token, stored.get("codex_account_id")


async def _codex_reset_request_with_refresh(provider_id: str, request_call):
    """Run one reset-credit request, refreshing and retrying once on a first 401."""
    from app.core.http_client import get_http_client

    client = get_http_client()
    access_token, account_id = _codex_reset_credentials(provider_id)
    try:
        return await request_call(access_token, account_id, client)
    except codex_rate_limit_resets.CodexResetCreditsError as exc:
        if not exc.is_unauthorized:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=_CODEX_RESET_UPSTREAM_DETAIL,
            ) from exc

    try:
        refreshed = await codex_token_refresh.refresh_on_401_once(
            provider_id,
            stale_access_token=access_token,
            client=client,
        )
    except Exception as exc:  # noqa: BLE001 - refresh internals remain private
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CODEX_RESET_RECONNECT_DETAIL,
        ) from exc
    if not refreshed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CODEX_RESET_RECONNECT_DETAIL,
        )

    try:
        access_token, account_id = _codex_reset_credentials(provider_id)
    except HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_CODEX_RESET_RECONNECT_DETAIL,
        ) from exc
    try:
        return await request_call(access_token, account_id, client)
    except codex_rate_limit_resets.CodexResetCreditsError as exc:
        if exc.is_unauthorized:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_CODEX_RESET_RECONNECT_DETAIL,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_CODEX_RESET_UPSTREAM_DETAIL,
        ) from exc


@router.get(
    "/{provider_id}/codex/rate-limit-reset-credits",
    response_model=CodexResetCreditsResponse,
)
async def codex_rate_limit_reset_credits(
    provider_id: str, token: str = Depends(require_admin)
):
    """Return the provider's normalized banked reset-credit inventory."""

    async def _request(access_token, account_id, client):
        return await codex_rate_limit_resets.get_reset_credits(
            access_token, account_id, client=client
        )

    return await _codex_reset_request_with_refresh(provider_id, _request)


@router.post(
    "/{provider_id}/codex/rate-limit-reset-credits/consume",
    response_model=CodexConsumeResetCreditResponse,
)
async def codex_consume_rate_limit_reset_credit(
    provider_id: str,
    body: CodexConsumeResetCreditRequest,
    token: str = Depends(require_admin),
):
    """Consume a specific banked reset credit with an idempotency key."""

    async def _request(access_token, account_id, client):
        return await codex_rate_limit_resets.consume_reset_credit(
            access_token,
            account_id,
            credit_id=body.credit_id,
            idempotency_key=body.idempotency_key,
            client=client,
        )

    return await _codex_reset_request_with_refresh(provider_id, _request)


# ============================================================================
# Grok / SuperGrok-subscription rate-limit reset credits
# ============================================================================

_GROK_RESET_UPSTREAM_DETAIL = (
    "The Grok reset-credit service is temporarily unavailable."
)
_GROK_RESET_RECONNECT_DETAIL = (
    "Reconnect the Grok provider before using reset credits."
)


def _is_grok_reset_provider(provider: Dict[str, Any]) -> bool:
    """SuperGrok OAuth rows. xAI API-key rows are ``provider_type='xai'``."""
    return (
        provider.get("provider_type") == "grok"
        or provider.get("auth_mode") == "grok"
    )


def _grok_reset_credentials(provider_id: str) -> str:
    """Load and decrypt one SuperGrok access token for an immediate request."""
    provider = db.get_model_provider(provider_id)
    if provider is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Model provider not found",
        )
    if not _is_grok_reset_provider(provider):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rate-limit reset credits require a Grok provider.",
        )
    if not encryption.is_encryption_ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credential storage is locked; local recovery is required.",
        )

    stored = db.get_codex_tokens(provider_id)
    if (
        not stored
        or stored.get("needs_reauth")
        or not stored.get("codex_access_token")
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_GROK_RESET_RECONNECT_DETAIL,
        )
    try:
        access_token = encryption.decrypt_value(stored["codex_access_token"])
    except Exception as exc:  # noqa: BLE001 - never expose credential failures
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_GROK_RESET_RECONNECT_DETAIL,
        ) from exc
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_GROK_RESET_RECONNECT_DETAIL,
        )
    return access_token


async def _try_grok_refresh_on_401(
    provider_id: str, access_token: str, client
) -> bool:
    """Refresh once on 401. Import-guarded until the Grok OAuth sibling lands."""
    try:
        from app.core import xai_token_refresh
    except ImportError:
        return False
    try:
        return bool(
            await xai_token_refresh.refresh_on_401_once(
                provider_id,
                stale_access_token=access_token,
                client=client,
            )
        )
    except Exception:  # noqa: BLE001 - refresh internals remain private
        return False


async def _grok_reset_request_with_refresh(provider_id: str, request_call):
    """Run one reset-credit request, refreshing and retrying once on a first 401."""
    from app.core.http_client import get_http_client

    client = get_http_client()
    access_token = _grok_reset_credentials(provider_id)
    try:
        return await request_call(access_token, client)
    except grok_rate_limit_resets.GrokResetCreditsError as exc:
        if not exc.is_unauthorized:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=_GROK_RESET_UPSTREAM_DETAIL,
            ) from exc

    refreshed = await _try_grok_refresh_on_401(provider_id, access_token, client)
    if not refreshed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_GROK_RESET_RECONNECT_DETAIL,
        )

    try:
        access_token = _grok_reset_credentials(provider_id)
    except HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_GROK_RESET_RECONNECT_DETAIL,
        ) from exc
    try:
        return await request_call(access_token, client)
    except grok_rate_limit_resets.GrokResetCreditsError as exc:
        if exc.is_unauthorized:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_GROK_RESET_RECONNECT_DETAIL,
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_GROK_RESET_UPSTREAM_DETAIL,
        ) from exc


@router.get(
    "/{provider_id}/grok/rate-limit-reset-credits",
    response_model=CodexResetCreditsResponse,
)
async def grok_rate_limit_reset_credits(
    provider_id: str, token: str = Depends(require_admin)
):
    """Return the provider's normalized banked reset-credit inventory."""

    async def _request(access_token, client):
        return await grok_rate_limit_resets.get_reset_credits(
            access_token, client=client
        )

    return await _grok_reset_request_with_refresh(provider_id, _request)


@router.post(
    "/{provider_id}/grok/rate-limit-reset-credits/consume",
    response_model=CodexConsumeResetCreditResponse,
)
async def grok_consume_rate_limit_reset_credit(
    provider_id: str,
    body: CodexConsumeResetCreditRequest,
    token: str = Depends(require_admin),
):
    """Consume a specific banked reset credit with an idempotency key."""

    async def _request(access_token, client):
        return await grok_rate_limit_resets.consume_reset_credit(
            access_token,
            credit_id=body.credit_id,
            idempotency_key=body.idempotency_key,
            client=client,
        )

    return await _grok_reset_request_with_refresh(provider_id, _request)


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

# provider_id -> (device_code, code_verifier, user_code, created_at_monotonic)
_pending_enrollments: Dict[str, Tuple[str, str, str, float]] = {}
# Grok/xAI enrollments are a separate map so a Codex and Grok flow on the same
# (or different) provider cannot clobber each other.
# provider_id -> (device_code, created_at_monotonic)
_pending_xai_enrollments: Dict[str, Tuple[str, float]] = {}
# Drop pending enrollments older than this (device codes expire upstream anyway).
_PENDING_TTL_SECONDS = 900


def _prune_pending() -> None:
    now = time.monotonic()
    stale = [
        pid for pid, (*_, ts) in _pending_enrollments.items()
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
            detail="Credential storage is locked; local recovery is required.",
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
        start.device_code, start.code_verifier, start.user_code, time.monotonic()
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
    device_code, code_verifier, user_code, _ = pending

    try:
        bundle = await codex_oauth.poll_device_code(device_code, code_verifier, user_code)
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


def _prune_pending_xai() -> None:
    now = time.monotonic()
    stale = [
        pid for pid, (*_, ts) in _pending_xai_enrollments.items()
        if now - ts > _PENDING_TTL_SECONDS
    ]
    for pid in stale:
        _pending_xai_enrollments.pop(pid, None)


def _seed_xai_preset_if_empty(provider_id: str) -> None:
    """Persist the xAI preset roster when the enrolled row has no models yet.

    A draft SuperGrok enrollment is created empty so the row exists for the
    device flow; seeding here makes it immediately usable without a second UI
    round-trip. A reconnect of a curated row is left untouched.
    """
    row = db.get_model_provider(provider_id)
    if row is None:
        return
    models = row.get("models_json") or []
    if models:
        return
    preset = model_catalog_presets.preset_models_for(
        {"provider_type": "grok", "auth_mode": "grok"}
    )
    if not preset:
        return
    updates: Dict[str, Any] = {"models_json": preset}
    if not row.get("default_model"):
        updates["default_model"] = "grok-4.6"
    if not row.get("small_fast_model"):
        updates["small_fast_model"] = "grok-4.3"
    db.update_model_provider(provider_id, **updates)


# ============================================================================
# xAI / SuperGrok device-code OAuth enrollment
# ============================================================================

@router.post("/{provider_id}/xai/device/start",
             response_model=CodexDeviceStartResponse)
async def xai_device_start(provider_id: str, token: str = Depends(require_admin)):
    """Start the SuperGrok device-code enrollment for a provider.

    Returns the ``user_code`` the admin enters at ``verification_uri``. When
    upstream supplies ``verification_uri_complete`` (prefilled code), that is
    returned as ``verification_uri`` so the UI "Open sign-in" link is ready.
    """
    if not encryption.is_encryption_ready():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Credential storage is locked; local recovery is required.",
        )
    if db.get_model_provider(provider_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    try:
        start = await xai_oauth.start_device_code()
    except xai_oauth.XaiAuthError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=f"Grok device-code start failed: {e.code}")

    _prune_pending_xai()
    _pending_xai_enrollments[provider_id] = (start.device_code, time.monotonic())
    return CodexDeviceStartResponse(
        user_code=start.user_code,
        verification_uri=start.verification_uri_complete or start.verification_uri,
    )


@router.post("/{provider_id}/xai/device/poll",
             response_model=CodexDevicePollResponse)
async def xai_device_poll(provider_id: str, token: str = Depends(require_admin)):
    """One poll step of the SuperGrok device-code enrollment.

    ``pending`` / ``slow_down`` → keep polling. On authorization: encrypt the
    access+refresh blobs, persist via ``set_codex_token_bundle`` with
    ``auth_mode='grok'``, seed the xAI preset if the row has no models, and
    return the fresh public row.
    """
    pending = _pending_xai_enrollments.get(provider_id)
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending Grok enrollment for this provider — start one first.",
        )
    device_code, _ = pending

    try:
        bundle = await xai_oauth.poll_device_code(device_code)
    except xai_oauth.XaiAuthPending as pend:
        return CodexDevicePollResponse(
            status="slow_down" if pend.slow_down else "pending"
        )
    except xai_oauth.XaiAuthError as e:
        _pending_xai_enrollments.pop(provider_id, None)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Grok enrollment failed: {e.code}")

    enc_access = _encrypt_token_or_400(bundle.access_token)
    enc_refresh = _encrypt_token_or_400(bundle.refresh_token)
    exp_dt = _xai_id_token_exp(bundle)
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
        auth_mode="grok",
    )
    _pending_xai_enrollments.pop(provider_id, None)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")

    _seed_xai_preset_if_empty(provider_id)

    full_row = _row_with_token_flags(provider_id)
    return CodexDevicePollResponse(status="complete", provider=_public_provider(full_row))


def _xai_id_token_exp(bundle: "xai_oauth.TokenBundle"):
    """Best-effort ``exp`` from the enrolled id_token (falls back to the access
    token) so the stored ``codex_id_token_expires_at`` reflects real expiry."""
    from app.core import xai_token_refresh
    return xai_token_refresh._access_token_exp(bundle.id_token or bundle.access_token)


@router.post("/{provider_id}/xai/clear-reauth",
             response_model=ModelProviderResponse)
async def xai_clear_reauth(provider_id: str, token: str = Depends(require_admin)):
    """Clear ONLY the ``needs_reauth`` flag on a Grok provider.

    Preserves the stored token bundle and ``auth_mode='grok'``.
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
        auth_mode=stored.get("auth_mode") or "grok",
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Model provider not found")
    full_row = _row_with_token_flags(provider_id)
    return _public_provider(full_row)
