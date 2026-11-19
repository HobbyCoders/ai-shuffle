"""Live Anthropic API catalog for the saved-key detail, not the CLI registry."""
from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

import httpx

from app.core.http_client import get_http_client

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
DEV_KEY = "dev-fake-anthropic-alpha"
_MAX_PAGES = 10


class CatalogError(Exception):
    """An error safe for display. Never forward an upstream body or headers."""


def _catalog_url(key: str) -> str:
    # A real credential always goes to Anthropic, even on a preview. The only
    # exception is the dev loop's literal fixture key on disposable data.
    if key == DEV_KEY:
        from app.preview_run import disposable_preview_data

        origin = urlsplit(os.environ.get("SHUFFLE_DEV_FAKE_AI_URL", ""))
        if (origin.scheme == "http" and origin.hostname == "127.0.0.1"
                and origin.port and not origin.username and not origin.password
                and origin.path in ("", "/") and not origin.query and not origin.fragment
                and disposable_preview_data()):
            return f"http://127.0.0.1:{origin.port}/anthropic/v1/models"
        raise CatalogError("The dev loop’s fake Anthropic model service is unavailable.")
    return ANTHROPIC_MODELS_URL


async def list_models(key: str, *, client: httpx.AsyncClient | None = None) -> list[dict[str, str]]:
    """Read every page without redirecting or making a billable model call."""
    client = client if client is not None else get_http_client()
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    params: dict[str, int | str] = {"limit": 100}
    cursors: set[str] = set()
    models: dict[str, dict[str, str]] = {}
    try:
        url = _catalog_url(key)
        async with asyncio.timeout(12):
            for _ in range(_MAX_PAGES):
                reply = await client.get(url, headers=headers, params=params, follow_redirects=False)
                if reply.status_code in (401, 403):
                    raise CatalogError("Anthropic did not accept this API key for listing models.")
                if not reply.is_success:
                    raise CatalogError("Anthropic could not list models. Try again.")
                page = reply.json()
                data = page.get("data") if isinstance(page, dict) else None
                if not isinstance(data, list) or len(data) > 100:
                    raise ValueError("Invalid page")
                for item in data:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid model")
                    model_id = item.get("id")
                    label = item.get("display_name")
                    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 300:
                        raise ValueError("Invalid model id")
                    models.setdefault(model_id, {
                        "id": model_id,
                        "label": label.strip()[:300] if isinstance(label, str) and label.strip() else model_id,
                    })
                has_more = page.get("has_more")
                if has_more is False:
                    return list(models.values())
                cursor = page.get("last_id")
                if (has_more is not True or not isinstance(cursor, str) or not cursor
                        or len(cursor) > 300 or cursor in cursors):
                    raise ValueError("Invalid page cursor")
                cursors.add(cursor)
                params["after_id"] = cursor
    except (httpx.HTTPError, TimeoutError, ValueError):
        raise CatalogError("Could not read the model list from Anthropic. Try again.") from None
    raise CatalogError("Anthropic returned too many pages of models.")
