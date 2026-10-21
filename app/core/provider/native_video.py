"""Verified SuperGrok native text-to-video. One submission, bounded async polling."""
import asyncio
import json
import re
from decimal import Decimal
from urllib.parse import urlencode, urlsplit

import httpx
from fluid.tools.registry import ToolOutcome, ToolSpec
from app.core.native_generation_settings import load, validate_generation, NativeGenerationSettings
from app.core.provider import supergrok_images
from app.core.provider.fluid_credentials import ProviderCredentialError
from app.db.async_database import run_sync

ENDPOINT = "https://api.x.ai/v1/videos/generations"
POLL_INTERVAL = 5
MAX_POLLS = 36
MAX_WAIT = 180
MAX_VIDEO_BYTES = 25 * 1024 * 1024

SPEC = ToolSpec(name="GenerateNativeVideo", description=(
    "Generate a video using this session's SuperGrok subscription and Native generation account settings. "
    "Verified only: grok-imagine-video-1.5-lite, 1 second, 480p, 16:9, silent text-to-video. "
    "Image-to-video and editing are not offered. The provider reports a USD cost: this is not free or unlimited. "
    "One submission is polled asynchronously for at most 180 seconds; no automatic resubmission or provider fallback. "
    "Returns a session-owned MP4 and video_url; link that URL in your reply."),
    input_schema={"type": "object", "properties": {"prompt": {"type": "string", "minLength": 1, "maxLength": 32000}},
                  "required": ["prompt"], "additionalProperties": False}, risk="write")


def http_client():
    return httpx.AsyncClient(timeout=30, follow_redirects=False)


def media_transport():
    # Use the transport directly for signed downloads: AsyncClient's INFO log
    # otherwise includes the complete private URL, even with no request headers.
    return httpx.AsyncHTTPTransport(retries=0)


def _failure(message, observed):
    return {"success": False, "error": message + " No other provider was used; the job was not resubmitted.", **observed}


def _observe(payload, observed):
    usage = payload.get("usage", {})
    ticks = usage.get("cost_in_usd_ticks") if isinstance(usage, dict) else None
    if isinstance(ticks, int) and not isinstance(ticks, bool) and ticks >= 0:
        observed["cost_in_usd_ticks"] = max(observed.get("cost_in_usd_ticks", 0), ticks)
        observed["reported_cost_usd"] = format(Decimal(observed["cost_in_usd_ticks"]) / Decimal(10_000_000_000), "f")


async def _download(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443)
            or not (host.endswith(".x.ai") or host.endswith(".xai.com"))):
        raise ValueError("Unapproved video media host")
    # No subscription credential/default auth/redirect forwarding to media hosts.
    async with media_transport() as transport:
        response = await transport.handle_async_request(httpx.Request("GET", url))
        try:
            if response.status_code != 200:
                raise ValueError("Video download failed")
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_VIDEO_BYTES:
                    raise ValueError("Video exceeds supported size")
        finally:
            await response.aclose()
    data = bytes(content)
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise ValueError("Expected MP4 output")
    return data


async def generate(provider_id, prompt, options, *, usage=None):
    observed = usage if usage is not None else {}
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        return _failure("A nonempty video prompt of at most 32000 characters is required.", observed)
    validate_generation("grok", NativeGenerationSettings(image=None, video=options))
    try:
        async with asyncio.timeout(MAX_WAIT):
            token = await supergrok_images.resolve_credential(provider_id)
            headers = {"Authorization": "Bearer " + token, "X-XAI-Token-Auth": "xai-grok-cli"}
            async with http_client() as client:
                response = await client.post(ENDPOINT, headers=headers, json={"prompt": prompt, **options.model_dump()})
                if response.status_code != 200:
                    return _failure(_provider_error(response.status_code), observed)
                payload = response.json()
                _observe(payload, observed)
                job = payload.get("request_id")
                if not isinstance(job, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", job) or token in job:
                    return _failure("SuperGrok returned no valid video job identifier.", observed)
                for _ in range(MAX_POLLS):
                    await asyncio.sleep(POLL_INTERVAL)
                    response = await client.get("https://api.x.ai/v1/videos/" + job, headers=headers)
                    if response.status_code not in (200, 202):
                        return _failure(_provider_error(response.status_code), observed)
                    payload = response.json()
                    _observe(payload, observed)
                    status = payload.get("status")
                    if status == "failed":
                        return _failure("SuperGrok refused or failed the video request.", observed)
                    if status == "done":
                        video = payload.get("video", {})
                        if video.get("respect_moderation") is False:
                            return _failure("SuperGrok refused the video content.", observed)
                        data = await _download(video.get("url", ""))
                        return {"success": True, "data": data, **observed}
                    if status != "pending":
                        return _failure("SuperGrok returned an unrecognized video job state.", observed)
                return _failure("SuperGrok video polling timed out; the job may still complete and incur a charge.", observed)
    except asyncio.CancelledError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        return _failure("SuperGrok video generation timed out; the job may still complete and incur a charge.", observed)
    except ProviderCredentialError:
        return _failure("The SuperGrok login has expired or needs reconnecting.", observed)
    except Exception:  # upstream bodies, URLs and exception details may contain credentials
        return _failure("SuperGrok video generation or download failed.", observed)


def _provider_error(status):
    return {401: "The SuperGrok login has expired; reconnect it.",
            403: "This SuperGrok login is not entitled to video generation.",
            429: "SuperGrok video rate limit reached; try later.",
            400: "SuperGrok refused the video request (content or options)."}.get(status, "SuperGrok video generation is unavailable.")


def make_handler(engine_supplier, session_supplier, *, provider_id_supplier):
    async def handler(args, ctx):
        from app.core.artifact_store import store_bytes
        provider_id, session_id = provider_id_supplier(), session_supplier()
        if not provider_id or not session_id:
            return ToolOutcome(text="A saved session with a bound SuperGrok account is required.", is_error=True)
        try:
            options = (await run_sync(load, provider_id)).video
            if options is None:
                return ToolOutcome(text="Native generation video is disabled or not yet verified for this account.", is_error=True)
        except ValueError:
            return ToolOutcome(text="Invalid Native generation video account settings.", is_error=True)
        engine, observed = engine_supplier(), {}
        try:
            result = await generate(provider_id, args.get("prompt"), options, usage=observed)
        finally:
            if observed:
                await engine.record_auxiliary_usage(
                    {"video_cost_in_usd_ticks": observed["cost_in_usd_ticks"], "reported_video_cost_usd": observed["reported_cost_usd"]},
                    model=options.model, turn_id=getattr(engine, "_active_turn_id", None), source="native_video_generation")
        if not result["success"]:
            return ToolOutcome(text=json.dumps(result), is_error=True)
        data = result.pop("data")
        task = asyncio.create_task(asyncio.to_thread(store_bytes, data, mime="video/mp4", filename="native-video.mp4", created_by_session=session_id))
        try:
            artifact = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task  # shared content-addressed artifacts must never be deleted
            raise
        except Exception:
            return ToolOutcome(text=json.dumps(_failure("The generated video could not be safely saved.", observed)), is_error=True)
        result.update(video_url=f"/api/v1/artifacts/{artifact['hash']}?" + urlencode({"session_id": session_id}),
                      mime_type="video/mp4", model=options.model, source="supergrok_subscription",
                      billing_note="Provider-reported USD cost; not a verified invoice or subscription quota debit. Not free or unlimited.")
        return ToolOutcome(text=json.dumps(result))
    return handler
