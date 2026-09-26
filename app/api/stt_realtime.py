"""Realtime (streaming) speech-to-text.

The batch route (``POST /settings/transcribe`` in ``settings_audio``) records a
whole utterance, uploads it and answers once. This module adds the *live* leg:
the browser streams raw PCM while it is still talking and gets partial text
back within a few hundred milliseconds.

Transport shape - deliberately a PROXY, not a browser-direct leg. The voice
*call* feature (``app.core.voice_bridge``) hands the browser an ephemeral
client secret and lets it hold a WebRTC leg straight to OpenAI, because that
call carries model audio back and the media path has to be peer-to-peer.
Transcription carries no return audio, so there is nothing to gain from that
complexity and two things to lose: the browser would need an ephemeral-secret
mint of its own, and it can only authenticate a WebSocket through the
``openai-insecure-api-key.<token>`` subprotocol hack. Proxying instead means
the key never leaves the server, the existing cookie/token auth applies
unchanged, and the client protocol is ours to keep small.

Client -> server:

* binary frame - raw PCM16 little-endian, mono, 24 kHz (``SAMPLE_RATE``)
* ``{"type": "commit"}`` - close the current segment now (push-to-talk release)
* ``{"type": "stop"}`` - graceful shutdown

Server -> client (always JSON text):

* ``ready`` - upstream accepted the model configuration, start sending audio
* ``speech_started`` / ``speech_stopped`` - server VAD edges, for the UI
* ``delta`` - more text for the in-flight segment
* ``final`` - the segment's finished transcript
* ``error`` - ``fatal`` says whether the stream is over
* ``closed`` - upstream hung up

Only OpenAI STT models can do this; a fal model answers the capability probe
with ``supported: false`` and the frontend stays on the batch path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, WebSocket
from fastapi.websockets import WebSocketState

from app.api.auth import require_auth
from app.api.ws_auth import authenticate_websocket
from app.api.ws_key_watch import watch_api_key
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

stt_stream_router = APIRouter(tags=["Settings"])

# OpenAI's realtime endpoint, entered with the transcription intent: the session
# only transcribes input audio, it never generates a response.
REALTIME_TRANSCRIBE_URL = "wss://api.openai.com/v1/realtime?intent=transcription"

# The audio contract with OpenAI. 24 kHz mono PCM16 is the realtime API's native
# input format, so the browser resamples once and nothing transcodes in between.
SAMPLE_RATE = 24000

# STT models that a transcription session accepts, mapped from our catalog id to
# the bare model name OpenAI wants. ``partials`` says whether the model streams
# ``.delta`` events mid-utterance - whisper-1 does not (it only answers once per
# VAD segment), and the UI shows a different affordance for it.
REALTIME_STT_MODELS: Dict[str, Dict[str, Any]] = {
    "openai/gpt-live-transcribe": {"name": "gpt-live-transcribe", "partials": True},
    "openai/gpt-4o-transcribe": {"name": "gpt-4o-transcribe", "partials": True},
    "openai/gpt-4o-mini-transcribe": {"name": "gpt-4o-mini-transcribe", "partials": True},
    "openai/whisper-1": {"name": "whisper-1", "partials": False},
}

# Server-side VAD. Tuned for dictation rather than conversation: a slightly
# longer silence window than the realtime default so a speaker who pauses to
# think does not get their sentence chopped in half.
TURN_DETECTION = {
    "type": "server_vad",
    "threshold": 0.5,
    "prefix_padding_ms": 300,
    "silence_duration_ms": 700,
}

# Hard bounds. A live microphone is an unbounded upstream bill, so the proxy
# stops it on its own rather than trusting the tab to close.
MAX_SESSION_SECONDS = 600
MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_FRAME_BYTES = 256 * 1024

# Ceiling on how long to keep reading after the client asks to stop, so the tail
# segment's transcript still arrives instead of being cut off by our own close.
# This is a give-up deadline, NOT a wait: the drain ends as soon as the tail has
# actually settled, which is normally a few hundred milliseconds.
DRAIN_TIMEOUT_SECONDS = 6.0

UPSTREAM_CONNECT_TIMEOUT = 12.0
SESSION_CONFIG_TIMEOUT = 12.0

# WebSocket close codes the client reads to decide whether to fall back to the
# batch path (4003 = "this model cannot stream") or surface an error.
CLOSE_UNSUPPORTED = 4003
CLOSE_NO_CREDENTIAL = 4004
CLOSE_UPSTREAM = 4005


# ----------------------------------------------------------------------------
# Capability
# ----------------------------------------------------------------------------


def _resolve_capability() -> Dict[str, Any]:
    """Whether the configured STT model can stream. Synchronous - use run_sync."""
    from app.api.settings_audio import STT_MODELS, _resolve_openai_api_key

    from app.core.voice_input_settings import get_voice_input_model
    model_id = get_voice_input_model()
    entry = STT_MODELS.get(model_id) or {}
    info = REALTIME_STT_MODELS.get(model_id)
    display = entry.get("name") or model_id

    # Also answers a question the BATCH path needs: may it upload the browser's
    # native recording, or must it re-encode to WAV first? Riding along here
    # keeps voice input to a single probe per press.
    accepts_compressed = bool(entry.get("accepts_compressed"))

    if not info:
        return {
            "supported": False,
            "model": model_id,
            "model_name": display,
            "partials": False,
            "sample_rate": SAMPLE_RATE,
            "accepts_compressed": accepts_compressed,
            "realtime_only": bool(entry.get("realtime_only")),
            "reason": (
                f"{display} transcribes uploaded audio only. Pick an OpenAI STT "
                f"model in Settings > Voice > Voice transcription for live transcription."
            ),
        }

    if not _resolve_openai_api_key():
        return {
            "supported": False,
            "model": model_id,
            "model_name": display,
            "partials": False,
            "sample_rate": SAMPLE_RATE,
            "accepts_compressed": accepts_compressed,
            "realtime_only": bool(entry.get("realtime_only")),
            "reason": (
                "No OpenAI provider with a stored API key is configured. "
                "Add one in Settings > Providers."
            ),
        }

    return {
        "supported": True,
        "model": model_id,
        "model_name": display,
        "partials": info["partials"],
        "sample_rate": SAMPLE_RATE,
        "accepts_compressed": accepts_compressed,
        "realtime_only": bool(entry.get("realtime_only")),
        "reason": None,
    }


@stt_stream_router.get("/transcribe/stream/capability")
async def transcribe_stream_capability(token: str = Depends(require_auth)):
    """Can the current STT model stream?

    The frontend probes this before opening the socket so it can choose live or
    batch without paying for a failed connection first.
    """
    return await run_sync(_resolve_capability)


# ----------------------------------------------------------------------------
# Session config ladder
# ----------------------------------------------------------------------------


def _session_update_ga(model_name: str, language: Optional[str]) -> Dict[str, Any]:
    """The GA realtime session shape (nested ``audio.input``)."""
    transcription: Dict[str, Any] = {"model": model_name}
    if language:
        if model_name == "gpt-live-transcribe":
            transcription["languages"] = [language]
        else:
            transcription["language"] = language
    if model_name == "gpt-live-transcribe":
        transcription["delay"] = "low"
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "noise_reduction": {"type": "near_field"},
                    "transcription": transcription,
                    # GPT Live streams deltas before the client commits each
                    # audio turn; its session rejects server_vad configuration.
                    "turn_detection": None if model_name == "gpt-live-transcribe" else dict(TURN_DETECTION),
                }
            },
        },
    }


def _session_update_legacy(model_name: str, language: Optional[str]) -> Dict[str, Any]:
    """The pre-GA ``transcription_session.update`` shape (flat fields).

    Sent only if the GA shape is rejected. The two revisions overlapped in the
    wild for months and an app pinned to one of them silently transcribes
    nothing, so the ladder is cheaper than guessing which one is live.
    """
    transcription: Dict[str, Any] = {"model": model_name}
    if language:
        transcription["language"] = language
    return {
        "type": "transcription_session.update",
        "session": {
            "input_audio_format": "pcm16",
            "input_audio_transcription": transcription,
            "turn_detection": dict(TURN_DETECTION),
        },
    }


# ----------------------------------------------------------------------------
# The proxy
# ----------------------------------------------------------------------------


class _ClientGone(Exception):
    """The browser closed its half of the socket."""


async def _send_client(websocket: WebSocket, payload: Dict[str, Any]) -> None:
    """Best-effort JSON to the browser.

    A dead client is not an error here - the pumps notice it on their own next
    read, and a raise from this helper would only mask that.
    """
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    try:
        await websocket.send_text(json.dumps(payload, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - the read pump owns disconnect handling
        pass


@stt_stream_router.websocket("/transcribe/stream")
async def transcribe_stream(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
):
    """Bridge the browser's microphone to an OpenAI realtime transcription session."""
    await websocket.accept()

    is_authenticated, _api_user = await authenticate_websocket(websocket, token)
    if not is_authenticated:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    capability = await run_sync(_resolve_capability)
    if not capability["supported"]:
        await _send_client(
            websocket,
            {
                "type": "error",
                "message": capability["reason"],
                "fatal": True,
                "unsupported": True,
            },
        )
        code = (
            CLOSE_NO_CREDENTIAL
            if capability["model"] in REALTIME_STT_MODELS
            else CLOSE_UNSUPPORTED
        )
        await websocket.close(code=code, reason="Streaming transcription unavailable")
        return

    from app.api.settings_audio import _resolve_openai_api_key

    api_key = await run_sync(_resolve_openai_api_key)
    if not api_key:
        await _send_client(
            websocket,
            {"type": "error", "message": "OpenAI API key unavailable.", "fatal": True},
        )
        await websocket.close(code=CLOSE_NO_CREDENTIAL, reason="No credential")
        return

    model_id = capability["model"]
    model_name = REALTIME_STT_MODELS[model_id]["name"]

    from websockets.asyncio.client import connect

    try:
        upstream = await asyncio.wait_for(
            connect(
                REALTIME_TRANSCRIBE_URL,
                additional_headers={"Authorization": f"Bearer {api_key}"},
                max_size=None,
                open_timeout=UPSTREAM_CONNECT_TIMEOUT,
            ),
            timeout=UPSTREAM_CONNECT_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the client verbatim
        logger.warning("STT stream: upstream connect failed: %s", exc)
        await _send_client(
            websocket,
            {
                "type": "error",
                "message": f"Could not reach OpenAI for live transcription: {exc}",
                "fatal": True,
            },
        )
        await websocket.close(code=CLOSE_UPSTREAM, reason="Upstream unavailable")
        return

    logger.info("STT stream: session open (model %s)", model_id)

    state = {
        "configured": False,
        "escalated": False,
        "audio_bytes": 0,
        "stopping": False,
    }
    started = time.monotonic()

    # Clearing after the stop commit acknowledges that all buffered audio has
    # been consumed. Transcription runs asynchronously, so also drain every
    # committed item before closing rather than guessing from a quiet interval.
    input_drained = False
    drain_ready = asyncio.Event()
    pending_items: set[str] = set()
    stop_commit_id = "stt-stop-commit"

    def check_drain() -> None:
        if input_drained and not pending_items:
            drain_ready.set()

    async with upstream:
        session_update = _session_update_ga(model_name, language)
        try:
            await upstream.send(json.dumps(session_update))
        except Exception as exc:  # noqa: BLE001
            logger.warning("STT stream: initial session.update failed: %s", exc)
            await _send_client(websocket, {
                "type": "error", "message": "Could not configure live transcription.", "fatal": True,
            })
            await websocket.close(code=CLOSE_UPSTREAM, reason="Session configuration failed")
            return

        async def pump_client_to_upstream() -> None:
            """Browser audio and control frames -> upstream buffer appends."""
            while True:
                message = await websocket.receive()

                if message["type"] == "websocket.disconnect":
                    raise _ClientGone()

                data = message.get("bytes")
                if data is not None:
                    if len(data) > MAX_FRAME_BYTES:
                        await _send_client(
                            websocket,
                            {
                                "type": "error",
                                "message": "Audio frame too large.",
                                "fatal": False,
                            },
                        )
                        continue
                    state["audio_bytes"] += len(data)
                    if state["audio_bytes"] > MAX_AUDIO_BYTES:
                        await _send_client(
                            websocket,
                            {
                                "type": "error",
                                "message": "Recording limit reached.",
                                "fatal": True,
                            },
                        )
                        raise _ClientGone()
                    if time.monotonic() - started > MAX_SESSION_SECONDS:
                        await _send_client(
                            websocket,
                            {
                                "type": "error",
                                "message": "Live transcription is capped at 10 minutes.",
                                "fatal": True,
                            },
                        )
                        raise _ClientGone()
                    await upstream.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(data).decode("ascii"),
                            }
                        )
                    )
                    continue

                text = message.get("text")
                if not text:
                    continue
                try:
                    control = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(control, dict):
                    continue

                kind = control.get("type")
                if kind == "commit":
                    await upstream.send(json.dumps({"type": "input_audio_buffer.commit"}))
                elif kind == "stop":
                    state["stopping"] = True
                    # Flush whatever is buffered so the tail utterance still
                    # transcribes, then let the drain window collect it.
                    try:
                        await upstream.send(
                            json.dumps({
                                "type": "input_audio_buffer.commit",
                                "event_id": stop_commit_id,
                            })
                        )
                        await upstream.send(json.dumps({"type": "input_audio_buffer.clear"}))
                    except Exception:  # noqa: BLE001
                        pass
                    return

        async def pump_upstream_to_client() -> None:
            """Upstream realtime events -> the small client protocol."""
            nonlocal input_drained
            events = upstream.__aiter__()
            deadline = time.monotonic() + SESSION_CONFIG_TIMEOUT
            while True:
                try:
                    raw = await asyncio.wait_for(
                        anext(events),
                        timeout=None if state["configured"] else max(0, deadline - time.monotonic()),
                    )
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    await _send_client(websocket, {
                        "type": "error", "message": "Live transcription configuration timed out.", "fatal": True,
                    })
                    return
                if isinstance(raw, bytes):
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue

                kind = event.get("type") or ""

                if kind == "input_audio_buffer.committed":
                    if event.get("item_id"):
                        pending_items.add(event["item_id"])
                    await _send_client(websocket, {
                        "type": "item_started",
                        "item_id": event.get("item_id"),
                        "previous_item_id": event.get("previous_item_id"),
                    })

                elif kind == "input_audio_buffer.cleared":
                    if state["stopping"]:
                        input_drained = True
                        check_drain()

                elif kind == "conversation.item.input_audio_transcription.delta":
                    delta = event.get("delta") or ""
                    if delta:
                        await _send_client(
                            websocket,
                            {
                                "type": "delta",
                                "item_id": event.get("item_id"),
                                "text": delta,
                            },
                        )

                elif kind == "conversation.item.input_audio_transcription.completed":
                    pending_items.discard(event.get("item_id"))
                    await _send_client(
                        websocket,
                        {
                            "type": "final",
                            "item_id": event.get("item_id"),
                            "text": (event.get("transcript") or "").strip(),
                        },
                    )
                    check_drain()

                elif kind == "conversation.item.input_audio_transcription.failed":
                    pending_items.discard(event.get("item_id"))
                    detail = (
                        (event.get("error") or {}).get("message")
                        or "Transcription failed."
                    )
                    await _send_client(
                        websocket,
                        {"type": "error", "message": detail, "fatal": False},
                    )
                    check_drain()

                elif kind == "input_audio_buffer.speech_started":
                    await _send_client(websocket, {
                        "type": "item_started", "item_id": event.get("item_id"),
                    })
                    await _send_client(websocket, {"type": "speech_started"})

                elif kind == "input_audio_buffer.speech_stopped":
                    await _send_client(websocket, {"type": "speech_stopped"})

                elif kind in (
                    "session.updated",
                    "transcription_session.updated",
                ):
                    if not state["configured"]:
                        state["configured"] = True
                        # An open socket does not mean this account/model accepted
                        # the session. Do not start capture on a rejected config.
                        await _send_client(websocket, {
                            "type": "ready", "model": model_id,
                            "model_name": capability["model_name"],
                            "partials": capability["partials"], "sample_rate": SAMPLE_RATE,
                            # The accepted session, not an earlier capability
                            # probe, determines the browser's silence handling.
                            "server_vad": session_update["session"]["audio"]["input"]["turn_detection"] is not None,
                        })

                elif kind == "error":
                    error = event.get("error") or {}
                    # VAD can consume the tail before Stop. Only the empty-buffer
                    # rejection of our own stop commit is an expected no-op.
                    if (
                        state["stopping"]
                        and error.get("code") == "input_audio_buffer_commit_empty"
                        and error.get("event_id") == stop_commit_id
                    ):
                        continue
                    detail = error.get("message") or "Realtime error."
                    # A rejected session.update before any ack is the shape
                    # mismatch the ladder exists for: try the other revision once
                    # and keep the failure off the user's screen.
                    if not state["configured"] and not state["escalated"] and model_name != "gpt-live-transcribe":
                        state["escalated"] = True
                        logger.info(
                            "STT stream: GA session shape rejected (%s) - "
                            "retrying legacy shape",
                            detail,
                        )
                        try:
                            await upstream.send(
                                json.dumps(_session_update_legacy(model_name, language))
                            )
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    logger.warning("STT stream: upstream error: %s", detail)
                    await _send_client(
                        websocket,
                        {"type": "error", "message": detail, "fatal": not state["configured"]},
                    )
                    if not state["configured"]:
                        return

        # A revoked API key loses the socket and the upstream session it pays
        # for (app.api.ws_key_watch).
        async with watch_api_key(websocket, on_revoke=lambda: upstream.close()):
            client_task = asyncio.create_task(pump_client_to_upstream())
            upstream_task = asyncio.create_task(pump_upstream_to_client())

            try:
                done, pending = await asyncio.wait(
                    {client_task, upstream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # A clean "stop" earns a drain window: the last commit is still
                # transcribing upstream and its ``final`` is the text the user
                # actually wants. Anything else (client vanished, upstream closed)
                # tears down immediately.
                if state["stopping"] and client_task in done and not upstream_task.done():
                    waiter = asyncio.ensure_future(drain_ready.wait())
                    try:
                        # Never cancel the pump while it is forwarding the tail.
                        await asyncio.wait(
                            {waiter, upstream_task},
                            timeout=DRAIN_TIMEOUT_SECONDS,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except Exception:  # noqa: BLE001 - the drain is best-effort
                        pass
                    finally:
                        waiter.cancel()
                        await asyncio.gather(waiter, return_exceptions=True)
                    pending = {t for t in pending if not t.done()}

                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

                for task in done:
                    exc = task.exception() if not task.cancelled() else None
                    if exc and not isinstance(exc, _ClientGone):
                        logger.warning("STT stream: pump failed: %s", exc)

            except _ClientGone:
                pass
            except Exception:  # noqa: BLE001 - a proxy bug must not 500 the socket
                logger.exception("STT stream: proxy failed")
            finally:
                for task in (client_task, upstream_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(client_task, upstream_task, return_exceptions=True)

    await _send_client(websocket, {"type": "closed"})
    if websocket.client_state == WebSocketState.CONNECTED:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
    logger.info(
        "STT stream: session closed after %.1fs (%d audio bytes)",
        time.monotonic() - started,
        state["audio_bytes"],
    )
