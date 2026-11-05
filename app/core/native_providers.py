"""Server-owned contract for native, key-authenticated Messages vendors.

plan_key is NOT a Claude subscription/OAuth auth mode. Its only credential
is the normal encrypted model_providers.auth_token, synchronized as API keys.
"""
from __future__ import annotations

import httpx

from fluid.providers.compatible import PROVIDERS, endpoint_root

# Sources: platform.kimi.ai/docs/api/messages; MiniMax text-anthropic-api;
# opencode.ai/v2/docs/console/{go,models,api/inference} (2026-11-05).
CAPABILITY = "native_messages_providers_v1"
DISPLAY = {"kimi": "Kimi", "minimax": "MiniMax", "opencode": "OpenCode"}
KEY_PAGES = {
    "kimi": {"plan_key": "https://www.kimi.com/code/console",
             "api_key": "https://platform.kimi.ai/console/api-keys"},
    "minimax": {"plan_key": "https://platform.minimax.io/console/plan",
                "api_key": "https://platform.minimax.io/user-center/basic-information/interface-key"},
    "opencode": {"plan_key": "https://opencode.ai/console", "api_key": "https://opencode.ai/console"},
}


def workers_requiring_update() -> list[str]:
    """All current members must understand these rows before they are created.

    Membership, not online presence, defines the set: offline/unknown peers
    can consume the synced credential later. Revoked members cannot sync.
    """
    from app.db import network as store
    from app.network import engine

    ctx = engine.context()
    if ctx is None:
        return []
    return [member.get("name") or member["device_id"]
            for member in store.list_members(include_revoked=False)
            if member["device_id"] != ctx.device_id
            and (member.get("capabilities") or {}).get(CAPABILITY) != 1]


def is_plan_key(row) -> bool:
    return row.get("provider_type") in PROVIDERS and row.get("auth_mode") == "plan_key"


def metadata(provider: str, mode: str | None) -> dict:
    mode = mode or "api_key"
    plan = mode == "plan_key"
    return {"provider_type": provider, "display_name": DISPLAY[provider], "auth_mode": "plan_key" if plan else None,
            "credential_kind": "subscription" if plan else "api_key",
            "slot": f"{provider}:{'subscription' if plan else 'key'}",
            "base_url": endpoint_root(provider, mode), "api_dialect": "anthropic",
            "key_page_url": KEY_PAGES[provider][mode], "usage_meter_available": False,
            "credential_label": ({"kimi": "Kimi Code membership", "minimax": "MiniMax M Plan", "opencode": "OpenCode Go"}[provider]
                                 if plan else {"kimi": "Kimi API (pay as you go)", "minimax": "MiniMax API (pay as you go)",
                                               "opencode": "OpenCode Console (pay as you go)"}[provider]),
            "key_prefixes": ["sk-cp-"] if provider == "minimax" and plan else []}


def request_headers(key: str, *, session_id: str | None = None) -> dict:
    from app.core.config import settings

    headers = {"x-api-key": key, "Authorization": f"Bearer {key}",
               "User-Agent": f"ai-shuffle/{settings.version}", "anthropic-version": "2023-06-01"}
    if session_id:
        headers["x-opencode-session"] = session_id
    return headers


def catalog_url(row) -> str | None:
    provider, mode = row.get("provider_type"), row.get("auth_mode")
    base = endpoint_root(provider, mode, row.get("base_url"))
    if provider == "kimi":
        # No documented Kimi Code models API in the membership docs; its
        # preset stands. Moonshot documents GET /v1/models (not /anthropic).
        if mode == "plan_key":
            return None
        return base.removesuffix("/anthropic") + "/v1/models"
    if provider == "opencode":
        return base.removesuffix("/anthropic") + "/v1/models"
    return base + "/v1/models"


class KeyCheckError(ValueError):
    pass


async def check_key(row, key: str, *, client: httpx.AsyncClient) -> bool:
    """Validate through authenticated Messages, not OpenCode's PUBLIC catalog.

    The one-token inference can consume a small amount of the owner's quota.
    Only a clear auth rejection refuses saving; outages do not erase a key.
    Never expose upstream bodies (which may echo the credential).
    """
    from app.core.model_catalog_presets import preset_models_for

    if not key:
        raise KeyCheckError("No provider key is saved. Add a key first.")
    models = row.get("models_json") or row.get("models") or preset_models_for(row) or []
    model = row.get("default_model") or (models[0]["id"] if models else None)
    if not model:
        raise KeyCheckError("Choose a model to check this provider key.")
    url = endpoint_root(row["provider_type"], row.get("auth_mode"), row.get("base_url")) + "/v1/messages"
    try:
        response = await client.post(url, headers=request_headers(
                                         key, session_id="ai-shuffle-key-check" if row["provider_type"] == "opencode" else None),
                                     json={"model": model, "messages": [{"role": "user", "content": "Hi"}],
                                           "max_tokens": 1, "stream": False}, timeout=20, follow_redirects=False)
    except httpx.HTTPError:
        return False
    try:
        error_type = response.json().get("error", {}).get("type")
    except (ValueError, AttributeError):
        error_type = None
    if response.status_code == 401 or error_type == "authentication_error":
        raise KeyCheckError("The provider rejected this key. Check its subscription/API kind and try again.")
    return 200 <= response.status_code < 300
