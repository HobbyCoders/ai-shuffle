"""SuperGrok images endpoint. Server-side subscription credentials only; one attempt."""
import asyncio

import httpx

from app.core.native_generation_settings import validate
from app.core.provider.fluid_credentials import ProviderCredentialError, _is_grok_subscription
from app.db import database
from app.db.async_database import run_sync

ENDPOINT = "https://api.x.ai/v1/images/generations"


async def subscription_kind(provider_id):
    if not provider_id:
        return None
    row = await run_sync(database.get_model_provider, provider_id, include_token=False)
    if row is None:
        raise ProviderCredentialError("The session's provider no longer exists.")
    from app.core.provider.fluid_credentials import _is_codex_subscription
    if _is_grok_subscription(row):
        return "grok"
    if _is_codex_subscription(row):
        return "chatgpt"
    return None


async def resolve_credential(provider_id):
    """Shared image/video resolver: no paid API keys or environment fallback."""
    from app.core.provider.fluid_credentials import _load_grok_access_token
    if await subscription_kind(provider_id) != "grok":
        raise ProviderCredentialError("A connected SuperGrok subscription is required.")
    return await _load_grok_access_token(provider_id)


def http_client():
    return httpx.AsyncClient(timeout=120, follow_redirects=False)


def _error(status=None, *, content_refused=False):
    if content_refused:
        return "SuperGrok refused the image content. No other provider was used."
    reason = {401: "The SuperGrok login has expired; reconnect it in Providers.",
              403: "This SuperGrok login is not entitled to image generation.",
              429: "SuperGrok image rate limit reached; try later.",
              400: "SuperGrok refused the image request (content or options)."}.get(
                  status, "SuperGrok image generation is unavailable; try later.")
    return reason + " No other provider was used."


async def generate(provider_id, prompt, options):
    validate("grok", options)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        return [], "A nonempty image prompt of at most 32000 characters is required."
    try:
        # The established resolver reads/decrypts the stored subscription login.
        # Never consult auth_token, API-key environment variables, or fal.ai.
        token = await resolve_credential(provider_id)
        client = http_client()
        try:
            response = await client.post(ENDPOINT,
                headers={"Authorization": "Bearer " + token, "X-XAI-Token-Auth": "xai-grok-cli"},
                json={"model": options.model, "prompt": prompt, "n": 1,
                      "resolution": options.size, "aspect_ratio": "1:1", "response_format": "b64_json"})
        finally:
            await client.aclose()
        if response.status_code != 200:
            # Match only known categories, never echo arbitrary upstream strings.
            try:
                error = response.json().get("error", {})
                content_refused = error.get("code") in ("content_policy_violation", "content_blocked", "safety_violation")
            except (ValueError, TypeError, AttributeError):
                content_refused = False
            return [], _error(response.status_code, content_refused=content_refused)
        data = response.json().get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0].get("b64_json"), str):
            return [], _error()
        return [{"result": data[0]["b64_json"], "status": "completed"}], None
    except asyncio.CancelledError:
        raise
    except ProviderCredentialError:
        return [], _error(401)
    except Exception:  # response/transport errors can include secrets: never echo or log them
        return [], _error()
