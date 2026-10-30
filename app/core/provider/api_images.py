"""Exact verified API-key image route; one submission, no retry or fallback."""
import asyncio

import httpx

from app.core import native_generation_settings as native
from app.core.native_image_verification import ENDPOINTS, verified
from app.db import database
from app.db.async_database import run_sync


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=120, follow_redirects=False)


async def generate(
    account: str, source: str, prompt: str, options: native.NativeImageSettings,
) -> tuple[list[dict[str, str]], str | None]:
    error = "API image generation did not confirm an image. No retry or other provider was used."
    try:
        # Recheck before reading a credential or opening a connection.
        record = verified(source)
        if record is None:
            return [], "This API image combination is not yet verified. Nothing was submitted."
        native.validate(source, options)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            return [], "A nonempty image prompt of at most 32000 characters is required."
        row = await run_sync(database.resolve_model_provider, account, include_token=True)
        if not row or not row.get("enabled", True) or native.provider_kind(row) != source:
            return [], error
        # Never send credentials to a custom/redirected host, nor use a plan login.
        endpoint = ENDPOINTS[source]
        if (row.get("base_url") or "").rstrip("/") != endpoint.rsplit("/images/", 1)[0]:
            return [], error
        from app.core.provider.fluid_credentials import _decrypt_auth_token
        token = await run_sync(_decrypt_auth_token, row)
        if not token:
            return [], error
        payload = {"model": options.model, "prompt": prompt, "n": 1}
        if source == "openai":
            payload.update(size=options.size, quality=options.quality, output_format=options.format)
        else:
            payload.update(resolution=options.size, aspect_ratio="1:1", response_format="b64_json")
        async with http_client() as client:
            response = await client.post(
                endpoint, headers={"Authorization": "Bearer " + token}, json=payload,
            )
        if response.status_code != 200:
            return [], error
        data = response.json().get("data")
        if (not isinstance(data, list) or len(data) != 1
                or not isinstance(data[0].get("b64_json"), str)):
            return [], error
        # The shared artifact writer validates the PNG/JPEG bytes before publication.
        return [{"result": data[0]["b64_json"], "status": "completed"}], None
    except asyncio.CancelledError:
        raise
    except Exception:
        # Provider errors may carry credentials and prompts. Never echo them.
        return [], error
