"""Server-owned contract for the Google Gemini API-key provider row.

A ``model_providers`` row of ``provider_type`` ``google`` holds one Google AI
Studio API key and runs on fluid's native ``gemini`` leg (the Interactions API,
``fluid/providers/gemini``). It is separate from the ``gemini_api_key`` setting,
which only feeds the media tools.

Rows sync to every network member, so creating one is gated on
:data:`CAPABILITY`: a member whose signed presence does not advertise it would
receive a row it cannot run.
"""
from __future__ import annotations

from typing import Any

import httpx

PROVIDER = "google"
CAPABILITY = "native_gemini_provider_v1"
DISPLAY_NAME = "Google Gemini"
API_ROOT = "https://generativelanguage.googleapis.com"
KEY_PAGE_URL = "https://aistudio.google.com/apikey"
#: Documented models list (aistudio.google.com/api-reference/models).
MODELS_URL = API_ROOT + "/v1beta/models"


def workers_requiring_update() -> list[str]:
    """Current members whose presence does not advertise :data:`CAPABILITY`.

    Same rule as the native Messages vendors: membership, not online
    presence, defines the set, since an offline peer syncs the row later.
    """
    from app.db import network as store
    from app.network import engine

    ctx = engine.context()
    if ctx is None:
        return []
    presence = engine.all_presence()
    return [member.get("name") or member["device_id"]
            for member in store.list_members(include_revoked=False)
            if member["device_id"] != ctx.device_id
            and (presence.get(member["device_id"], {}).get("capabilities") or {}).get(CAPABILITY) != 1]


def metadata() -> dict[str, Any]:
    return {
        "provider_type": PROVIDER, "display_name": DISPLAY_NAME, "auth_mode": None,
        "credential_kind": "api_key", "slot": "google:key", "base_url": API_ROOT,
        "api_dialect": None, "key_page_url": KEY_PAGE_URL,
        "usage_meter_available": False, "credential_label": "Google AI Studio API key",
        "key_prefixes": [],
    }


def request_headers(key: str) -> dict[str, str]:
    from app.core.config import settings

    return {"x-goog-api-key": key, "User-Agent": f"ai-shuffle/{settings.version}"}


def models_url(row: dict[str, Any] | None = None) -> str:
    base = ((row or {}).get("base_url") or "").strip().rstrip("/")
    if not base or base == API_ROOT:
        return MODELS_URL
    for suffix in ("/v1beta/interactions", "/v1/interactions", "/interactions"):
        base = base.removesuffix(suffix)
    if base.endswith(("/v1beta", "/v1")):
        return base + "/models"
    return base + "/v1beta/models"


def parse_models(payload: Any) -> list[dict[str, Any]]:
    """Text-generation models from a ``GET /v1beta/models`` page.

    Keeps ``gemini-*`` models that list ``generateContent`` (the Interactions
    API serves the same models); drops embeddings, image, audio and TTS.
    """
    out: list[dict[str, Any]] = []
    for item in (payload or {}).get("models") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").removeprefix("models/")
        methods = item.get("supportedGenerationMethods") or []
        if not name.startswith("gemini-") or "generateContent" not in methods:
            continue
        if any(tag in name for tag in ("-image", "-tts", "-audio", "-live", "embedding")):
            continue
        model: dict[str, Any] = {"id": name, "name": item.get("displayName") or name}
        limit = item.get("inputTokenLimit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            model["context_length"] = limit
        out.append(model)
    return out


class KeyCheckError(ValueError):
    pass


async def check_key(key: str, *, client: httpx.AsyncClient, row: dict[str, Any] | None = None) -> bool:
    """Validate the key with the free models list (no inference spend).

    Google returns 400 ``API_KEY_INVALID`` for a bad key and 403 for one
    without access. Only those refuse saving; an outage does not erase a key.
    Upstream bodies are never surfaced (they may echo the request).
    """
    if not key:
        raise KeyCheckError("No provider key is saved. Add a key first.")
    try:
        response = await client.get(models_url(row), headers=request_headers(key),
                                    params={"pageSize": 1}, timeout=20, follow_redirects=False)
    except httpx.HTTPError:
        return False
    if response.status_code in (400, 401, 403):
        try:
            body = response.text
        except Exception:  # noqa: BLE001 - only the classification matters
            body = ""
        if response.status_code != 400 or "API_KEY_INVALID" in body or "API key not valid" in body:
            raise KeyCheckError("Google rejected this API key. Check it in Google AI Studio and try again.")
    return 200 <= response.status_code < 300
