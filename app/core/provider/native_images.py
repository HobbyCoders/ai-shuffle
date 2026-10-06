"""Native Responses image generation using the current session's provider."""

import asyncio
import base64
import binascii
import json
import re
import struct
import zlib
from contextlib import aclosing
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from fluid.messages import Message, Text
from fluid.providers.base import ProviderRequest
from fluid.tools.registry import ToolImage, ToolOutcome, ToolSpec

MAX_IMAGE_BYTES = 12 * 1024 * 1024

SPEC = ToolSpec(
    name="GenerateNativeImage",
    description=(
        "Generate an image with the current session's Responses provider and credential. "
        "Uses the provider-native image_generation tool; no fal.ai credential is needed. "
        "Model/account support is verified by the provider when called, not assumed from the model name. "
        "Use only for a requested image; the configured provider may charge for generation. "
        "Session cost estimates do not include native image charges. "
        "Never treat a rejection here as evidence that fal.ai is unavailable, or vice versa. "
        "Returns a saved image and image_url: embed it inline in your reply. "
        "Omit image_model to use the provider's default, or pass the exact image model requested by the user."
    ),
    input_schema={"type": "object", "properties": {
        "prompt": {"type": "string", "minLength": 1, "maxLength": 32000},
        "image_model": {"type": "string", "minLength": 1, "maxLength": 128},
        "size": {"type": "string", "pattern": r"^(auto|[1-9][0-9]{1,4}x[1-9][0-9]{1,4})$"},
        "quality": {"type": "string", "enum": ["auto", "low", "medium", "high", "xhigh", "max"]},
    }, "required": ["prompt"], "additionalProperties": False},
    risk="write",
)


def _error(status=None, payload=None):
    detail = ""
    try:
        body = json.loads((payload or {}).get("message", ""))
        error = body.get("error") or {}
        code = error.get("code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            detail = f", code {code}"
    except (ValueError, TypeError, AttributeError):
        pass
    return (f"The configured provider did not complete native image generation"
            f"{f' (HTTP {status}{detail})' if status else ''}. "
            "Check this model/account's image-tool support and the requested options. "
            "No other provider was used. fal.ai availability is independent.")


def _validate_png(data):
    # Validate the bounded PNG container without adding an optional image-decoder
    # dependency to session startup. Browsers decode the raster for display.
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Expected a PNG image")
    offset, has_pixels = 8, False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + length + 12
        if end > len(data):
            break
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:end - 4]
        checksum = int.from_bytes(data[end - 4:end], "big")
        if zlib.crc32(kind + payload) != checksum:
            break
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                break
            width, height = struct.unpack("!II", payload[:8])
            if not width or not height or width * height > 64_000_000:
                break
        has_pixels |= kind == b"IDAT" and length > 0
        if kind == b"IEND":
            if length == 0 and end == len(data) and has_pixels:
                return
            break
        offset = end
    raise ValueError("Incomplete or invalid PNG image")


def _decode_image(item):
    encoded = item.get("result")
    if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 4 // 3:
        raise ValueError("Native image exceeds the supported size")
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Native image exceeds the supported size")
    _validate_png(data)
    return data, encoded


def _save_artifacts(session_id, items):
    from app.core.artifact_store import store_bytes

    # Validate all results first. Chat mode never writes a caller-selected path.
    decoded = [_decode_image(item) for item in items]
    return [(store_bytes(data, mime="image/png", filename="native-image.png", created_by_session=session_id)["hash"], encoded)
            for data, encoded in decoded]


def _save_images(cwd, items):
    root = Path(cwd).resolve(strict=True)
    directory = root / "generated-images"
    directory.mkdir(exist_ok=True)
    directory = directory.resolve(strict=True)
    directory.relative_to(root)
    saved = []
    try:
        for item in items:
            data, encoded = _decode_image(item)
            path = directory / f"native-{uuid4().hex}.png"
            with path.open("xb") as stream:
                saved.append((path, encoded))
                stream.write(data)
        return saved
    except BaseException:
        for path, _ in saved:
            path.unlink(missing_ok=True)
        raise


async def stream_images(provider, request, items, usage):
    """Run one native image request, filling ``items`` and ``usage`` in place.

    Returns a sanitized warning or ``None``. The containers are the caller's so
    observations made before a cancellation stay with the caller, which records
    them in its own ``finally``. Also used by the subscription image tools that
    chats on other providers get (app.core.provider.subscription_images).
    """
    warning = None
    try:
        async with aclosing(provider.stream(request)) as chunks:
            async for chunk in chunks:
                if chunk.kind == "image_generation":
                    if chunk.payload.get("status") == "completed" and chunk.payload.get("result"):
                        items.append(chunk.payload)
                    else:
                        warning = _error()
                elif chunk.kind in {"usage", "message_start", "provider_request"}:
                    observed = chunk.payload.get("usage", {})
                    for key, value in observed.items():
                        if isinstance(value, int) and not isinstance(value, bool):
                            usage[key] = max(usage.get(key, 0), value)
                    if chunk.payload.get("provider_requests"):
                        usage["provider_requests"] = chunk.payload["provider_requests"]
                elif chunk.kind == "error":
                    warning = _error(chunk.payload.get("status"), chunk.payload)
                    break
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - request errors may contain credentials
        warning = _error()
    return warning


def make_handler(engine_supplier, session_supplier, *, chat_mode=False):
    async def generate(args, ctx):
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            return ToolOutcome(text="A nonempty image prompt of at most 32000 characters is required.", is_error=True)
        engine = engine_supplier()
        provider = getattr(engine, "provider", None)
        if getattr(provider, "name", None) != "openai_responses":
            return ToolOutcome(text="Native image generation requires this session's Responses provider. No other provider was used.", is_error=True)
        session_id = session_supplier()
        if not session_id:
            return ToolOutcome(text="The session must be saved before generating an image.", is_error=True)
        tool = {"type": "image_generation", "output_format": "png", "partial_images": 0}
        for key in ("image_model", "size", "quality"):
            if key not in args:
                continue
            value = args[key]
            definition = SPEC.input_schema["properties"][key]
            if (not isinstance(value, str) or not value.strip() or len(value) > 128
                    or ("enum" in definition and value not in definition["enum"])
                    or ("pattern" in definition and re.fullmatch(definition["pattern"], value) is None)):
                return ToolOutcome(text=f"Invalid native image {key}.", is_error=True)
            tool["model" if key == "image_model" else key] = value
        model = engine.model
        turn_id = getattr(engine, "_active_turn_id", None)
        request = ProviderRequest(model=model, messages=[Message("user", [Text(prompt)])],
                                  tools=[tool], extra={"tool_choice": {"type": "image_generation"}})
        items, usage, warning = [], {}, None
        try:
            warning = await stream_images(provider, request, items, usage)
        finally:
            if items:
                usage["generated_images"] = len(items)
            if usage:
                await engine.record_auxiliary_usage(usage, model=model, turn_id=turn_id,
                                                    source="native_image_generation")
        if not items:
            return ToolOutcome(text=warning or _error(), is_error=True)
        # Keep file I/O off the event loop, but drain a cancelled write before
        # removing exactly the files it created. An unfinished tool publishes none.
        task = asyncio.create_task(asyncio.to_thread(
            _save_artifacts if chat_mode else _save_images,
            session_id if chat_mode else ctx.cwd, items,
        ))
        try:
            saved = await asyncio.shield(task)
        except asyncio.CancelledError:
            saved = await task
            if not chat_mode:
                for path, _ in saved:
                    path.unlink(missing_ok=True)
            # Content-addressed artifacts can predate this call and be shared.
            # Never delete them to clean up a cancelled tool's unpublished result.
            raise
        except (OSError, ValueError, binascii.Error):
            return ToolOutcome(text="The provider returned an image that could not be safely saved. No fallback provider was used.", is_error=True)
        results = ([{"image_url": f"/api/v1/artifacts/{digest}?" + urlencode({"session_id": session_id})}
                    for digest, _ in saved] if chat_mode else
                   [{"image_url": "/api/generated-images/by-path?" + urlencode({"path": str(path), "session_id": session_id}),
                     "path": str(path)} for path, _ in saved])
        return ToolOutcome(
            text=json.dumps({"success": True, "source": "native_responses", "model": model,
                             "images": results, "image_url": results[0]["image_url"], "warning": warning}),
            images=tuple(ToolImage(media_type="image/png", data=data) for _, data in saved),
        )
    return generate
