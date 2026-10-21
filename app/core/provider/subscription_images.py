"""Image tools on the owner's ChatGPT subscription, for chats on any provider.

``GenerateNativeImage`` (``native_images``) uses the session's own Responses
provider, so only chats on an OpenAI Responses model have it. When Settings
names the ChatGPT subscription as the image model (``image_model`` /
``image_edit_model`` set to :data:`SUBSCRIPTION_IMAGE_MODEL`), the ai-tools
image tools (``app.mcp.ai_tools_sdk``) route here instead of to fal.ai, so a
chat on Claude, OpenRouter or a voice call gets the same image path (#1796).

Credential boundary: these handlers run in the app process. The ChatGPT
sign-in is resolved from the encrypted ``model_providers`` row by the same
resolver the native lane uses and handed straight to fluid's Responses client.
It never enters the session, the Node tool worker's environment, or any tool
result; provider errors are reduced to an HTTP status and a safe error code.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from fluid.messages import Image, Message, Text
from fluid.providers.base import ProviderRequest

from app.core.provider import native_images

logger = logging.getLogger(__name__)

# The Settings value that selects this path. ``chatgpt-subscription`` uses
# OpenAI's default image model; ``chatgpt-subscription/<id>`` asks for <id>.
SUBSCRIPTION_IMAGE_MODEL = "chatgpt-subscription"
PROVIDER_NAME = "OpenAI image generation on the ChatGPT subscription signed in on this worker"
SOURCE = "chatgpt_subscription"

MAX_INPUT_IMAGES = 14
MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024
_INPUT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "gif": "image/gif"}
# Settings > Models > Image aspect ratios this path accepts, as Responses sizes.
_SIZES = {"1:1": "1024x1024", "3:2": "1536x1024", "2:3": "1024x1536"}

NOT_SIGNED_IN = (
    "Image generation is set to the ChatGPT subscription, but no ChatGPT account is "
    "signed in on this worker (or it needs to sign in again). Sign in under Settings > "
    "Providers > OpenAI, or pick another image model under Settings > Models. "
    "No other image provider was used."
)


def is_subscription_model(model_id: Optional[str]) -> bool:
    """Whether a configured image model id selects this path."""
    return isinstance(model_id, str) and (
        model_id == SUBSCRIPTION_IMAGE_MODEL or model_id.startswith(SUBSCRIPTION_IMAGE_MODEL + "/")
    )


def image_model_of(configured: str) -> Optional[str]:
    """The exact OpenAI image model to request, or ``None`` for OpenAI's default."""
    tail = configured[len(SUBSCRIPTION_IMAGE_MODEL) + 1:] if configured.startswith(SUBSCRIPTION_IMAGE_MODEL + "/") else ""
    return tail.strip() or None


def model_label(configured: str) -> str:
    model = image_model_of(configured)
    return f"OpenAI image model {model}" if model else "OpenAI's default image model"


def describe(action: str, configured: str) -> str:
    """Tool description naming the configured provider and model only."""
    return (f"{action} with {PROVIDER_NAME}, using {model_label(configured)} "
            f"(configured image model '{configured}'). This is the only image provider "
            "configured here: do not name or suggest another provider's models.")


def model_argument_description(configured: str) -> str:
    return (f"Leave this out. The only model offered is the configured '{configured}' "
            f"({model_label(configured)}); any other model id is refused.")


def refusal(requested: Any, configured: str) -> Optional[str]:
    """A plain refusal when the call names a model the configured provider does not offer."""
    if requested in (None, ""):
        return None
    if requested in (configured, image_model_of(configured)):
        return None
    return (f"Image generation here is set to {PROVIDER_NAME} ({model_label(configured)}, "
            f"configured as '{configured}'). '{requested}' is not one of its models, so nothing "
            "was generated and no other provider was used. Call again without a model to use "
            "the configured one; only the owner can change the image model, in Settings > Models.")


def _result(payload: Dict[str, Any]) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}


def _failure(message: str) -> dict:
    return _result({"success": False, "error": message, "provider_used": SOURCE})


# ---------------------------------------------------------------------------
# Credential seams (stubbed in tests)
# ---------------------------------------------------------------------------

async def _signed_in_row() -> Optional[Dict[str, Any]]:
    """The newest enabled ChatGPT-subscription row whose sign-in is usable.

    Public columns only: the encrypted token blobs never leave the query.
    """
    from app.core.provider.fluid_credentials import _is_codex_subscription
    from app.db import model_providers
    from app.db.async_database import run_sync

    rows = await run_sync(model_providers.list_usable_model_providers)
    return next((row for row in rows if _is_codex_subscription(row)), None)


def _text_model(row: Dict[str, Any]) -> Optional[str]:
    """The row's chosen default model, else its first curated model.

    The Responses API needs a text model to call the image tool; the native
    lane uses the session's model, here there is none on this provider.
    """
    from app.core.provider_agent import curated_model_ids, default_model_for_provider

    ids = curated_model_ids(row)
    return default_model_for_provider(row) or (ids[0] if ids else None)


async def _open_client(provider_id: str) -> Tuple[Any, Any]:
    """``(client, injected_http_client)`` for the row, built like a session's."""
    from app.core.provider.harness_adapter import _build_provider_client

    return await _build_provider_client("openai_responses", SimpleNamespace(extra={"provider_id": provider_id}))


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def _read_input_image(path_arg: Any, cwd: Optional[str]) -> Image:
    if not isinstance(path_arg, str) or not path_arg.strip():
        raise ValueError("An image path is required.")
    path = Path(path_arg).expanduser()
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    media_type = _INPUT_TYPES.get(path.suffix.lower().lstrip("."))
    if media_type is None:
        raise ValueError(f"'{path_arg}' is not a PNG, JPEG, WebP or GIF image file.")
    if not path.is_file():
        raise ValueError(f"Image file not found: {path_arg}")
    if path.stat().st_size > MAX_INPUT_IMAGE_BYTES:
        raise ValueError(f"'{path_arg}' is larger than 20 MB.")
    return Image(media_type=media_type, data=base64.b64encode(path.read_bytes()).decode("ascii"))


async def run(prompt: Any, configured: str, *, image_paths=(), cwd: Optional[str] = None,
              tool_env: Optional[Dict[str, str]] = None) -> dict:
    """Generate (no images) or edit/compose (with images) on the subscription."""
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        return _failure("A nonempty image prompt of at most 32000 characters is required.")
    if len(image_paths) > MAX_INPUT_IMAGES:
        return _failure(f"At most {MAX_INPUT_IMAGES} images can be passed.")
    try:
        images = [_read_input_image(p, cwd) for p in image_paths]
    except (OSError, ValueError) as exc:
        return _failure(str(exc) if isinstance(exc, ValueError) else f"Could not read the image: {exc.strerror}")

    row = await _signed_in_row()
    if not row:
        return _failure(NOT_SIGNED_IN)
    text_model = _text_model(row)
    if not text_model:
        return _failure("The ChatGPT provider on this worker has no model chosen to run the image "
                        "request. Pick a default model under Settings > Providers > OpenAI. "
                        "No other image provider was used.")

    from app.core.provider.fluid_credentials import ProviderCredentialError

    try:
        client, http_client = await _open_client(str(row["id"]))
    except ProviderCredentialError:
        return _failure(NOT_SIGNED_IN)

    tool: Dict[str, Any] = {"type": "image_generation", "output_format": "png", "partial_images": 0}
    model = image_model_of(configured)
    if model:
        tool["model"] = model
    size = _SIZES.get((tool_env or {}).get("IMAGE_ASPECT_RATIO") or "")
    if size:
        tool["size"] = size
    request = ProviderRequest(model=text_model, messages=[Message("user", [Text(prompt), *images])],
                              tools=[tool], extra={"tool_choice": {"type": "image_generation"}})
    items: list = []
    usage: Dict[str, Any] = {}
    try:
        warning = await native_images.stream_images(client, request, items, usage)
    finally:
        if http_client is not None:
            await http_client.aclose()
    if not items:
        return _failure(warning or native_images._error())
    # Same rule as the native tool: a cancelled call publishes no file.
    task = asyncio.create_task(asyncio.to_thread(native_images._save_images, cwd or ".", items))
    try:
        saved = await asyncio.shield(task)
    except asyncio.CancelledError:
        for path, _ in await task:
            path.unlink(missing_ok=True)
        raise
    except (OSError, ValueError, binascii.Error):
        return _failure("The provider returned an image that could not be safely saved. "
                        "No other provider was used.")
    results = [{"image_url": f"/api/generated-images/by-path?path={quote(str(path), safe='')}",
                "file_path": str(path), "filename": path.name, "mime_type": "image/png"}
               for path, _ in saved]
    payload: Dict[str, Any] = {"success": True, **results[0], "provider_used": SOURCE,
                               "model_used": model or "openai-default"}
    if len(results) > 1:
        payload["images"] = results
    if warning:
        payload["warning"] = warning
    return _result(payload)
