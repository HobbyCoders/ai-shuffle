"""Whether the selected OpenAI API key can use GPT-Live-1.1.

The engine appears in Settings only after ``GET /v1/models/gpt-live-1.1``
answers 200 for the provider a call would run on. Results are cached per
provider (6 h when found, 1 h on 404, 10 min on any other answer) and tied to
a fingerprint of the key, so a re-entered key is checked again. The key itself
is never logged or stored here.

Synchronous readers (settings, call creation) only read the cache; the async
``refresh_*`` helpers fill it before those reads.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from app.core.voice_models import LIVE_11_MODEL, LIVE_MODEL

logger = logging.getLogger(__name__)

MODEL_URL = f"https://api.openai.com/v1/models/{LIVE_11_MODEL}"
FOUND_TTL_S = 6 * 3600
MISSING_TTL_S = 3600
ERROR_TTL_S = 600
PROBE_TIMEOUT_S = 8.0

_now = time.monotonic  # Patched by tests.

# provider_id -> (available, key fingerprint, monotonic expiry)
_cache: dict[str, tuple[bool, str, float]] = {}


def _fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


async def _fetch_status(api_key: str) -> int:
    """HTTP status of the model lookup. Raises on network failure."""
    from app.core.http_client import get_http_client
    response = await get_http_client().get(
        MODEL_URL, headers={"Authorization": f"Bearer {api_key}"},
        timeout=PROBE_TIMEOUT_S, follow_redirects=False,
    )
    return response.status_code


async def probe(provider_id: str, api_key: str) -> bool:
    """Whether ``api_key`` sees GPT-Live-1.1, from the cache when still fresh."""
    fingerprint = _fingerprint(api_key)
    entry = _cache.get(provider_id)
    if entry and entry[1] == fingerprint and entry[2] > _now():
        return entry[0]
    try:
        status = await _fetch_status(api_key)
    except Exception as exc:  # Network errors can carry request headers; log the type only.
        logger.info("Voice: GPT-Live-1.1 lookup failed (%s); hidden for now", type(exc).__name__)
        status = None
    available = status == 200
    ttl = FOUND_TTL_S if available else MISSING_TTL_S if status == 404 else ERROR_TTL_S
    if status not in (200, 404):
        logger.info("Voice: GPT-Live-1.1 lookup answered %s; hidden for now", status)
    _cache[provider_id] = (available, fingerprint, _now() + ttl)
    return available


def cached(provider_id: Optional[str]) -> bool:
    entry = _cache.get(provider_id or "")
    return bool(entry and entry[0] and entry[2] > _now())


def selected_provider_row(pinned_provider_id: Optional[str]) -> Optional[dict]:
    """The OpenAI API-key row a GPT Live call would run on, or None. Sync."""
    from fastapi import HTTPException
    from app.api.voice import _resolve_provider_row
    try:
        return _resolve_provider_row(None, pinned_provider_id, None, LIVE_MODEL)
    except HTTPException:
        return None


def selected_available(pinned_provider_id: Optional[str]) -> bool:
    """Cached answer for the row a GPT Live call would run on. Sync."""
    row = selected_provider_row(pinned_provider_id)
    return bool(row) and cached(row["id"])


async def refresh(pinned_provider_id: Optional[str]) -> bool:
    """Look up (or reuse) the answer for the row a GPT Live call would run on."""
    from app.db.async_database import run_sync
    from app.core.provider.fluid_credentials import _decrypt_auth_token
    row = await run_sync(selected_provider_row, pinned_provider_id)
    if not row:
        return False
    try:
        api_key = await run_sync(_decrypt_auth_token, row)
    except Exception:
        api_key = None
    if not api_key:
        return False
    return await probe(row["id"], api_key)


async def refresh_from_settings(pinned_provider_id: Optional[str] = None, *, only_if_stored: bool = False) -> None:
    """Warm the cache for the stored voice provider pin.

    ``only_if_stored`` limits the lookup to installs whose saved engine is
    GPT-Live-1.1, so call start never adds a request for other engines.
    """
    from app.db import database
    from app.db.async_database import run_sync
    if only_if_stored and await run_sync(database.get_system_setting, "voice_model") != LIVE_11_MODEL:
        return
    if pinned_provider_id is None:
        pinned_provider_id = (await run_sync(database.get_system_setting, "voice_provider_id") or "").strip() or None
    await refresh(pinned_provider_id)


def clear_cache() -> None:
    _cache.clear()
